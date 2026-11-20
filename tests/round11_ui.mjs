import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { makeUI } from "./ui_harness.mjs";

for (const lang of ["en", "ja"]) {
  const { context, ui, document } = makeUI();
  for (const file of ["en", "ja", "core"]) vm.runInContext(fs.readFileSync(new URL(`../web/i18n/${file}.js`, import.meta.url), "utf8"), context);
  ui.S.lang = lang;
  ui.S.bots = [{ id: "b1", name: "Builder", initials: "B" }];
  const root = document.createElement("div"); root.setAttribute("id", "app"); document.body.appendChild(root);
  const files = ["image", "html", "pdf", "text", "file"].map((kind, n) => ({
    id: "f-" + String(n).repeat(32), name: ["screen.png", "index.html", "report.pdf", "notes.txt", "bundle.zip"][n],
    kind, caption: "<img src=x onerror=alert(1)>", size: 1024,
    url: "https://evil.example/ignored"
  }));
  for (const role of ["bot", "system"]) {
    const message = ui.messageEl({ id: "m1", role, bot: "b1", text: "Result", status: "done", session: "s-test",
      ...(role === "system" ? { result: { status: "review", duration_s: 1 } } : {}), attachments: files });
    root.appendChild(message);
    assert.equal(message.querySelectorAll(".attachment-card").length, 5);
    assert.equal(message.querySelectorAll("img").filter(x => x.getAttribute("src")?.startsWith("/api/attachments/")).length, 1);
    assert.ok(message.querySelectorAll("a").every(a => !a.getAttribute("href").includes("evil.example")));
    assert.equal(message.querySelectorAll(".attachment-caption")[0].textContent, files[0].caption);
    assert.ok(!message.querySelectorAll(".attachment-caption")[0].html);
    const cards = message.querySelectorAll(".attachment-card");
    const download = cards[0].querySelectorAll("a")[0];
    assert.equal(download.getAttribute("href"), `/api/attachments/${files[0].id}/download`);
    assert.match(download.textContent, lang === "ja" ? /ファイルを保存/ : /Save file/);
    await cards[0].querySelector(".attachment-image").emit("click");
    let sh = ui.activeSheets.at(-1);
    assert.equal(sh.panel.querySelector(".attachment-full").getAttribute("src"), `/api/attachments/${files[0].id}/content`);
    sh.close();
    assert.equal(cards[1].querySelectorAll("button").length, 0);
    assert.equal(cards[1].querySelectorAll("a").length, 1);
    assert.equal(cards[1].querySelector("a").getAttribute("href"), `/api/attachments/${files[1].id}/download`);
    assert.equal(cards[1].querySelector("a").getAttribute("download"), files[1].name);
    assert.match(cards[1].textContent, lang === "ja" ? /HTML のファイル/ : /HTML file/);
    assert.match(cards[1].textContent, /index.html/);
    assert.match(cards[1].textContent, /1 KB/);
    assert.equal(message.querySelectorAll("iframe").length, 0);
    assert.equal(cards[2].querySelector("a").getAttribute("target"), "_blank");
    assert.equal(cards[4].querySelectorAll("button").length, 0);
  }
  const invalid = ui.messageEl({ id: "bad", role: "bot", bot: "b1", text: "Files", attachments: [{ id: "../../secret", kind: "image" }], attachment_errors: ["file_refused"] });
  assert.equal(invalid.querySelectorAll(".attachment-card").length, 0);
  assert.match(invalid.textContent, lang === "ja" ? /添付できなかった/ : /could not be attached/);
}
console.log("Round 11 UI: inline images, enlargement, download-only HTML, downloads, safe metadata and delivery errors passed in English and Japanese.");
