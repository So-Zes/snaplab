"""Fresh-install security and provider regressions; all credentials are synthetic."""
import ctypes
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from helpers import fake_credentials, make_app
from snaplab import config, credentials, setup
from snaplab.server import make_server
from snaplab.models import derive
from tools.launch import launch

KEY = "synthetic-round16-key-not-a-real-credential"


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.creds = fake_credentials()
        self.creds.__enter__()
        self.addCleanup(self.creds.__exit__, None, None, None)
        self.app, _, _ = make_app(tmp=self.temp.name, local_mode=True, api={"model": "configure-model", "api_key_env": "SNAPLAB_API_KEY"})
        self.addCleanup(self.app.archive.close)
        self.body = {"base_url": "https://api.example.com/v1", "model": "ordinary-model", "key": KEY}

    def test_fresh_defaults_do_not_select_snapscribe(self):
        cfg = config.load(Path(self.temp.name)/"none")
        self.assertEqual(cfg["slots"]["count"], 2)
        self.assertEqual(cfg["api"]["api_key_env"], "SNAPLAB_API_KEY")
        self.assertEqual(cfg["models"]["default"]["effort_style"], "none")
        self.assertNotIn("snapscribe", cfg["providers"])

    def test_legacy_explicit_settings_keep_their_behavior(self):
        cfg = config.load(Path(self.temp.name)/"none", overrides={"api": {"base_url": "https://legacy.example/v1", "model": "qwen3.8-27b", "api_key_env": "LEGACY_KEY"}, "slots": {"count": 6, "dir": str(Path(self.temp.name)/"shared")}})
        self.assertFalse(cfg["local_mode"])
        self.assertTrue(cfg["require_tailscale_user"])
        self.assertEqual(cfg["models"]["qwen"]["effort_style"], "chat_template_kwargs")
        self.assertEqual(cfg["slots"]["count"], 6)

    def test_existing_partial_config_keeps_legacy_defaults(self):
        path = Path(self.temp.name) / "old-config.json"
        path.write_text(json.dumps({"api":{"base_url":"https://legacy.example/v1"}}),encoding="utf-8")
        cfg = config.load(path)
        self.assertEqual(cfg["api"]["model"],"qwen3.8-27b")
        self.assertEqual(cfg["api"]["api_key_env"],"SNAPSCRIBE_API_KEY")
        self.assertEqual(cfg["routing"]["default"],"qwen")
        self.assertEqual(cfg["slots"]["count"],3)

    def test_existing_non_qwen_model_keeps_internal_routing_names(self):
        path = Path(self.temp.name) / "old-config.json"
        path.write_text(json.dumps({"api":{"model":"ordinary-model"},"routing":{"default":"qwen"}}),encoding="utf-8")
        cfg = config.load(path)
        from snaplab.models import check
        self.assertEqual(check(cfg),[])
        self.assertEqual(cfg["models"]["qwen"]["model"],"ordinary-model")
        self.assertEqual(cfg["models"]["qwen"]["effort_style"],"none")

    def test_only_qwen_gets_qwen_options(self):
        self.assertEqual(setup.validate_fields(self.body)[3], "none")
        self.assertEqual(setup.validate_fields({**self.body,"model":"qwen3.8-27b"})[3], "chat_template_kwargs")

    def test_generic_model_omits_reasoning_fields_and_redirectable_credentials(self):
        from snaplab.llm import LLM
        def upstream(req, **kw):
            body=json.loads(req.data)
            self.assertNotIn("chat_template_kwargs",body)
            self.assertNotIn("reasoning_content",body["messages"][0])
            self.assertEqual(req.get_header("Authorization"),"Bearer "+KEY)
            self.assertNotIn("Authorization",req.headers)
            return io.BytesIO(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        llm=LLM({"base_url":"https://api.example/v1","model":"ordinary","api_key_env":"TEST16"},self.app.slots)
        with patch.object(llm,"key",return_value=KEY),patch("snaplab.llm.open_request",side_effect=upstream):
            self.assertEqual(llm.stream_chat([{"role":"assistant","content":"Hi","reasoning_content":"private thought"}]).content,"OK")

    def test_urls_cannot_leak_a_key_or_send_it_over_remote_http(self):
        for url in ("http://api.example.com/v1", "https://user:pass@api.example/v1", "https://api.example/v1?key="+KEY,
                    "https://api.example/"+KEY, "https://api.example/#x", "file:///tmp/x", "https://api.example:bad/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                setup.validate_fields({**self.body,"base_url":url})

    def test_failed_probe_does_not_save_anything(self):
        with patch.object(setup, "check_connection", side_effect=ValueError("connection failed")), patch.object(credentials,"write_credential") as save:
            with self.assertRaises(ValueError): setup.configure(self.app,self.body)
            save.assert_not_called()
        self.assertFalse(self.app.store.path("provider.json").exists())

    def test_success_stores_only_metadata_and_reload_uses_it(self):
        with patch.object(setup,"check_connection") as probe, patch.object(credentials,"write_credential") as save:
            setup.configure(self.app,self.body)
        probe.assert_called_once()
        self.assertEqual(save.call_args.args[1],KEY)
        target=save.call_args.args[0]
        self.assertNotEqual(target,"SNAPSCRIBE_API_KEY")
        raw=self.app.store.path("provider.json").read_text(encoding="utf-8")
        self.assertNotIn(KEY,raw)
        self.assertNotIn(KEY,self.app.log_path.read_text(encoding="utf-8") if self.app.log_path.exists() else "")
        cfg=config.load(Path(self.temp.name)/"none",overrides={"local_mode":True,"data_dir":self.app.cfg["data_dir"]})
        self.assertEqual(cfg["api"]["api_key_env"],target)
        self.assertEqual(cfg["models"]["default"]["effort_style"],"none")
        with self.assertRaises(ValueError):setup.configure(self.app,self.body)

    def test_credential_failure_does_not_complete_setup(self):
        with patch.object(setup,"check_connection"), patch.object(credentials,"write_credential",side_effect=ValueError("store unavailable")):
            with self.assertRaises(ValueError):setup.configure(self.app,self.body)
        self.assertTrue(setup.needed(self.app))

    def test_windows_writer_passes_blob_only_to_credential_api(self):
        api=Mock()
        seen={}
        def write(ptr, flags):
            value=ctypes.cast(ptr,ctypes.POINTER(credentials._Credential)).contents
            seen["target"]=value.TargetName
            seen["key"]=ctypes.string_at(value.CredentialBlob,value.CredentialBlobSize).decode("utf-16-le")
            return True
        api.CredWriteW=Mock(side_effect=write)
        with patch.object(credentials.os,"name","nt"),patch.object(credentials,"_advapi32",return_value=api):
            credentials.write_credential("synthetic-target",KEY)
        self.assertEqual(seen,{"target":"synthetic-target","key":KEY})

    def test_launcher_preserves_existing_nonempty_folder(self):
        folder=Path(self.temp.name)/"existing"
        folder.mkdir()
        p=folder/"settings.json"
        p.write_text("unchanged",encoding="utf-8")
        with self.assertRaises(ValueError):launch(folder,19460,False)
        self.assertEqual(p.read_text(encoding="utf-8"),"unchanged")


class LocalHTTPTest(SetupTest):
    def setUp(self):
        super().setUp()
        self.server=make_server(self.app,"127.0.0.1",0)
        self.port=self.server.server_port
        self.app.cfg["listen_port"]=self.port
        self.app.public_url=f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self,path="/",method="GET",body=None,headers=None):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=4)
        h={"Content-Type":"application/json","X-SnapLab":"1",**(headers or {})}
        conn.request(method,path,json.dumps(body).encode() if body is not None else None,h)
        r=conn.getresponse()
        result=r.status,dict(r.getheaders()),r.read()
        conn.close()
        return result

    def pair(self):
        token,_=self.app.auth.new_pairing("test",1)
        status,h,_=self.request("/api/pair","POST",{"token":token})
        self.assertEqual(status,200)
        self.assertIn("HttpOnly",h["Set-Cookie"])
        self.assertIn("SameSite=Strict",h["Set-Cookie"])
        return h["Set-Cookie"].split(";",1)[0]

    def test_host_rebinding_and_forwarded_requests_are_denied(self):
        for h in ({"Host":"attacker.example"},{"X-Forwarded-For":"127.0.0.1"},{"Tailscale-User-Login":"owner@example.com"}):
            self.assertEqual(self.request("/healthz",headers=h)[0],403)

    def test_local_still_needs_pairing_and_same_origin(self):
        self.assertEqual(self.request("/api/bootstrap")[0],401)
        cookie=self.pair()
        self.assertEqual(self.request(headers={"Cookie":cookie})[0],303)
        with patch.object(setup,"check_connection") as check:
            status,_,_=self.request("/api/setup/provider","POST",self.body,{"Cookie":cookie,"Origin":"https://attacker.example"})
            self.assertEqual(status,403)
            check.assert_not_called()
        status,_,_=self.request("/api/onboarding","POST",{"name":"Test","lang":"en"},{"Cookie":cookie})
        self.assertEqual(status,400)

    def test_local_cookie_authenticates_live_events(self):
        cookie=self.pair()
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=4)
        conn.request("GET","/api/events",headers={"Cookie":cookie})
        response=conn.getresponse()
        self.assertEqual(response.status,200)
        self.assertEqual(response.readline(),b"retry: 2000\n")
        for _ in range(4):response.readline()
        self.app.bus.publish("round16.event",{"working":True})
        self.assertIn(b'"working":true',response.readline())
        self.app.auth.revoke("all")
        self.app.bus.publish("round16.stop",{})
        response.close();conn.close()

    def test_provider_redirect_is_not_followed(self):
        class Redirect( __import__('http.server',fromlist=['BaseHTTPRequestHandler']).BaseHTTPRequestHandler):
            seen=[]
            def log_message(self,*args):pass
            def do_POST(self):
                self.seen.append(self.path)
                self.send_response(307)
                self.send_header("Location","/leak")
                self.end_headers()
        from http.server import ThreadingHTTPServer
        srv=ThreadingHTTPServer(("127.0.0.1",0),Redirect)
        threading.Thread(target=srv.serve_forever,daemon=True).start()
        try:
            with self.assertRaises(ValueError):setup.check_connection(f"http://127.0.0.1:{srv.server_port}","ordinary",KEY,"none",self.app.slots)
            self.assertEqual(Redirect.seen,["/chat/completions"])
        finally:srv.shutdown();srv.server_close()
