// SnapLab Setup for Windows. Built with the C# compiler that ships with Windows (.NET Framework 4.x), so the
// language level is C# 5: no string interpolation, no "?." and no expression-bodied members.
//
// What it does, in order (see docs/installer.md):
//   1. Check this PC (64-bit Windows 10 2004 or later).
//   2. Administrator part, only if something is missing: turn on WSL, install Python and Git. This runs in a second
//      copy of this program started with "Run as administrator" (--admin-steps), which reports through a state file.
//   3. Download SnapLab (git clone), 4. its Python environment, 5. the Linux workspace (tools/setup_wsl.py),
//   6. Start menu, "start with Windows" and the Apps & features entry, 7. open SnapLab in the browser.
// Steps 3-7 run as the signed-in user, never as administrator.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("SnapLab Setup")]
[assembly: AssemblyProduct("SnapLab")]
[assembly: AssemblyCompany("So-Zes")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

namespace SnapLabSetup
{
    class Options
    {
        public string Repo = "https://github.com/So-Zes/snaplab.git";
        public string Branch = "main";
        public string Name = "SnapLab";
        public string Profile, AppDir, DataDir, Screenshots, StateFile, Need;
        public int Port = 18445;
        public bool AdminSteps, Resume, NoElevate, SkipWsl, Quiet, NoLaunch, NoStartup, NoShortcut, Uninstall, DeleteData, Auto;

        public static Options Parse(string[] args)
        {
            var o = new Options();
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i];
                Func<string> next = () => { i++; if (i >= args.Length) throw new ArgumentException(a + " needs a value"); return args[i]; };
                switch (a)
                {
                    case "--repo": o.Repo = next(); break;
                    case "--branch": o.Branch = next(); break;
                    case "--name": o.Name = next(); break;
                    case "--profile": o.Profile = next(); break;
                    case "--app-dir": o.AppDir = next(); break;
                    case "--data-dir": o.DataDir = next(); break;
                    case "--port": o.Port = int.Parse(next()); break;
                    case "--screenshots": o.Screenshots = next(); break;
                    case "--admin-steps": o.AdminSteps = true; o.StateFile = next(); break;
                    case "--need": o.Need = next(); break;
                    case "--resume": o.Resume = true; break;
                    case "--no-elevate": o.NoElevate = true; break;
                    case "--skip-wsl": o.SkipWsl = true; break;
                    case "--quiet": o.Quiet = true; break;
                    case "--no-launch": o.NoLaunch = true; break;
                    case "--no-startup": o.NoStartup = true; break;
                    case "--no-shortcut": o.NoShortcut = true; break;
                    case "--uninstall": o.Uninstall = true; break;
                    case "--delete-data": o.DeleteData = true; break;
                    case "--auto": o.Auto = true; break;  // presses the main button on each page (for screenshots in tests)
                    default: throw new ArgumentException("Unknown option " + a);
                }
            }
            if (string.IsNullOrEmpty(o.Profile)) o.Profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            string home = Path.Combine(o.Profile, ".snaplab");
            if (string.IsNullOrEmpty(o.AppDir)) o.AppDir = Path.Combine(home, "app");
            if (string.IsNullOrEmpty(o.DataDir)) o.DataDir = Path.Combine(home, "data");
            return o;
        }

        public string Home { get { return Path.Combine(Profile, ".snaplab"); } }
        public string Venv { get { return Path.Combine(AppDir, ".venv"); } }
        public string VenvPython { get { return Path.Combine(Venv, "Scripts", "python.exe"); } }
        public string VenvPythonW { get { return Path.Combine(Venv, "Scripts", "pythonw.exe"); } }
        public string KeptExe { get { return Path.Combine(Home, Name + "-Setup.exe"); } }
    }

    static class Log
    {
        static readonly object Gate = new object();
        public static string FilePath;
        public static Action<string> Listener;

        public static void Write(string line)
        {
            lock (Gate)
            {
                try { File.AppendAllText(FilePath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss ") + line + "\r\n", Encoding.UTF8); }
                catch (Exception) { }
            }
            var l = Listener;
            if (l != null) l(line);
        }
    }

    static class Shell
    {
        // Runs a program without a window. Output goes to the log (and to `output` when given).
        public static int Run(string file, string args, string cwd, int timeoutMs, StringBuilder output, Encoding encoding)
        {
            var psi = new ProcessStartInfo(file, args);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            if (encoding != null) { psi.StandardOutputEncoding = encoding; psi.StandardErrorEncoding = encoding; }
            else { psi.StandardOutputEncoding = Encoding.UTF8; psi.StandardErrorEncoding = Encoding.UTF8; }
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            if (cwd != null) psi.WorkingDirectory = cwd;
            Log.Write("> " + Path.GetFileName(file) + " " + args);
            using (var p = new Process())
            {
                p.StartInfo = psi;
                DataReceivedEventHandler h = (s, e) =>
                {
                    if (e.Data == null) return;
                    string line = e.Data.Replace("\0", "");
                    if (line.Trim().Length == 0) return;
                    Log.Write("  " + line);
                    if (output != null) lock (output) output.AppendLine(line);
                };
                p.OutputDataReceived += h;
                p.ErrorDataReceived += h;
                try { p.Start(); }
                catch (Exception ex) { Log.Write("  could not start: " + ex.Message); return -1; }
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
                if (!p.WaitForExit(timeoutMs))
                {
                    try { p.Kill(); } catch (Exception) { }
                    Log.Write("  stopped after " + (timeoutMs / 1000) + " s");
                    return -2;
                }
                p.WaitForExit();
                Log.Write("  exit " + p.ExitCode);
                return p.ExitCode;
            }
        }

        public static string FreshPath()
        {
            string machine = Registry.GetValue(@"HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\Session Manager\Environment", "Path", "") as string;
            string user = Registry.GetValue(@"HKEY_CURRENT_USER\Environment", "Path", "") as string;
            return Environment.ExpandEnvironmentVariables((machine ?? "") + ";" + (user ?? ""));
        }

        public static void RefreshPath() { Environment.SetEnvironmentVariable("PATH", FreshPath()); }

        public static string Which(string exe)
        {
            foreach (string dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
            {
                if (dir.Trim().Length == 0) continue;
                // The WindowsApps python.exe is a Store shortcut, not Python.
                if (exe.StartsWith("python", StringComparison.OrdinalIgnoreCase) && dir.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                try
                {
                    string c = Path.Combine(dir.Trim().Trim('"'), exe);
                    if (File.Exists(c)) return c;
                }
                catch (Exception) { }
            }
            return null;
        }

        public static string FindPython()
        {
            string py = Which("py.exe");
            if (py != null)
                foreach (string v in new[] { "-3.13", "-3.12", "-3.11" })
                {
                    string exe = AskPython(py, v);
                    if (exe != null) return exe;
                }
            var candidates = new List<string>();
            string onPath = Which("python.exe");
            if (onPath != null) candidates.Add(onPath);
            string pf = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            foreach (string v in new[] { "313", "312", "311" })
            {
                candidates.Add(Path.Combine(pf, "Python" + v, "python.exe"));
                candidates.Add(Path.Combine(local, "Programs", "Python", "Python" + v, "python.exe"));
            }
            foreach (string c in candidates)
                if (File.Exists(c))
                {
                    string exe = AskPython(c, null);
                    if (exe != null) return exe;
                }
            return null;
        }

        static string AskPython(string exe, string flag)
        {
            var sb = new StringBuilder();
            string args = (flag != null ? flag + " " : "") + "-c \"import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 11) else 1)\"";
            if (Run(exe, args, null, 60000, sb, null) != 0) return null;
            string last = sb.ToString().Trim().Split('\n').Last().Trim();
            return File.Exists(last) ? last : null;
        }

        public static string FindGit()
        {
            string git = Which("git.exe");
            if (git != null) return git;
            string c = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Git", "cmd", "git.exe");
            return File.Exists(c) ? c : null;
        }

        public static bool WslWorks()
        {
            return Run("wsl.exe", "--status", null, 120000, null, Encoding.Unicode) == 0
                && Run("wsl.exe", "--version", null, 120000, null, Encoding.Unicode) == 0;
        }

        public static string Quote(string s) { return "\"" + s.Replace("\"", "\\\"") + "\""; }
    }

    // Messages in English and Japanese; other languages use English.
    static class T
    {
        static readonly bool Ja = System.Globalization.CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "ja";
        static readonly Dictionary<string, string[]> S = new Dictionary<string, string[]>
        {
            { "title", new[] { "SnapLab Setup", "SnapLab セットアップ" } },
            { "welcome", new[] { "Install SnapLab", "SnapLab をインストール" } },
            { "resume", new[] { "Continue installing SnapLab", "SnapLab のインストールを続ける" } },
            { "intro", new[] { "SnapLab runs your bots and a coding workspace on this PC. You use it in this PC's browser, and later from your phone.",
                               "SnapLab は、Bot と作業用の環境をこの PC の中で動かします。この PC のブラウザから使え、あとでスマホからも使えます。" } },
            { "what", new[] { "What setup installs", "インストールするもの" } },
            { "what1", new[] { "Windows Subsystem for Linux (WSL), if it is not turned on yet", "Windows Subsystem for Linux（WSL）。まだ有効でない場合だけ" } },
            { "what2", new[] { "Python and Git, if they are missing", "Python と Git。入っていない場合だけ" } },
            { "what3", new[] { "SnapLab and its private Linux workspace (about 3 GB)", "SnapLab と、専用の Linux の作業環境（約 3 GB）" } },
            { "why", new[] { "Why administrator permission is needed", "管理者の権限が要る理由" } },
            { "why1", new[] { "Turning on WSL and installing Python and Git for this PC change Windows itself, so Windows asks for administrator permission once. SnapLab itself runs as you, never as an administrator.",
                              "WSL を有効にすることと、Python と Git をこの PC に入れることは Windows そのものを変えるので、Windows が一度だけ管理者の権限を求めます。SnapLab 自体は管理者ではなく、あなたの権限で動きます。" } },
            { "time", new[] { "Setup takes 10 to 30 minutes. Keep this PC connected to the internet.", "インストールには 10〜30 分かかります。PC をインターネットにつないだままにしてください。" } },
            { "install", new[] { "Install", "インストール" } },
            { "continue", new[] { "Continue", "続ける" } },
            { "cancel", new[] { "Cancel", "キャンセル" } },
            { "close", new[] { "Close", "閉じる" } },
            { "denied", new[] { "Windows did not give administrator permission, so nothing was changed. Click Install to try again.",
                                "Windows で管理者の権限が許可されなかったので、何も変えていません。もう一度インストールを押してください。" } },
            { "installing", new[] { "Installing SnapLab", "SnapLab をインストールしています" } },
            { "s.check", new[] { "Check this PC", "この PC を確認する" } },
            { "s.admin", new[] { "Turn on WSL and install Python and Git", "WSL を有効にし、Python と Git を入れる" } },
            { "s.download", new[] { "Download SnapLab", "SnapLab をダウンロードする" } },
            { "s.prepare", new[] { "Prepare SnapLab", "SnapLab を準備する" } },
            { "s.workspace", new[] { "Prepare the Linux workspace (the longest step)", "Linux の作業環境を準備する（いちばん長い手順）" } },
            { "s.shortcut", new[] { "Add SnapLab to the Start menu", "スタートメニューに SnapLab を追加する" } },
            { "s.open", new[] { "Open SnapLab", "SnapLab を開く" } },
            { "details", new[] { "Show details", "詳しい記録を見る" } },
            { "waitadmin", new[] { "Waiting for administrator permission...", "管理者の権限を待っています..." } },
            { "restart.title", new[] { "Restart to continue", "再起動して続ける" } },
            { "restart.body", new[] { "Windows needs a restart to finish turning on WSL. After you sign in again, SnapLab Setup opens by itself and continues.",
                                      "WSL を有効にし終えるには、Windows の再起動が必要です。再起動してサインインすると、SnapLab のセットアップが自動で開いて続きを進めます。" } },
            { "restart.now", new[] { "Restart now", "今すぐ再起動" } },
            { "restart.later", new[] { "Later", "あとで" } },
            { "done.title", new[] { "SnapLab is ready", "SnapLab の準備ができました" } },
            { "done.body", new[] { "SnapLab opened in your browser. Paste your Snapscribe API key there to finish.",
                                   "ブラウザで SnapLab が開きました。Snapscribe の API キーを貼り付けると使い始められます。" } },
            { "done.pc", new[] { "SnapLab runs on this PC, so it works only while the PC is on and awake. It starts by itself when you sign in to Windows.",
                                 "SnapLab はこの PC の中で動くので、使えるのは PC の電源が入っていて、スリープしていないあいだだけです。Windows にサインインすると自動で起動します。" } },
            { "done.again", new[] { "To open it later, choose SnapLab in the Start menu.", "あとで開くときは、スタートメニューの SnapLab を選んでください。" } },
            { "done.noworkspace", new[] { "The coding workspace is not ready yet, so SnapCode stays off for now. Chat works. Run setup again later to retry. Reason: ",
                                          "作業用の Linux 環境の準備が終わらなかったので、SnapCode はまだ使えません。チャットは使えます。あとでセットアップをもう一度実行すると、続きから準備します。理由: " } },
            { "done.open", new[] { "Open SnapLab", "SnapLab を開く" } },
            { "fail.title", new[] { "Setup stopped", "セットアップが止まりました" } },
            { "fail.body", new[] { "Nothing you had before was removed. You can fix the problem below and run setup again; it continues where it stopped.",
                                   "前からあったものは何も消していません。下の問題を解決してからもう一度実行すると、止まったところから続けます。" } },
            { "tryagain", new[] { "Try again", "もう一度試す" } },
            { "stop.ask", new[] { "Stop setup? You can run it again later and it continues where it stopped.", "セットアップを止めますか？あとでもう一度実行すると、止まったところから続けます。" } },
            { "err.os", new[] { "SnapLab needs 64-bit Windows 10 (version 2004 or later) or Windows 11.", "SnapLab には 64 ビット版の Windows 10（バージョン 2004 以降）か Windows 11 が必要です。" } },
            { "err.wsl", new[] { "WSL could not be turned on. Make sure virtualization is enabled in the PC's BIOS/UEFI settings, then try again.",
                                 "WSL を有効にできませんでした。PC の BIOS/UEFI の設定で仮想化が有効になっているかを確認して、もう一度試してください。" } },
            { "err.python", new[] { "Python could not be installed. Check the internet connection and try again.", "Python を入れられませんでした。インターネットの接続を確認して、もう一度試してください。" } },
            { "err.git", new[] { "Git could not be installed. Check the internet connection and try again.", "Git を入れられませんでした。インターネットの接続を確認して、もう一度試してください。" } },
            { "err.download", new[] { "SnapLab could not be downloaded. Check the internet connection and try again.", "SnapLab をダウンロードできませんでした。インターネットの接続を確認して、もう一度試してください。" } },
            { "err.folder", new[] { "The SnapLab folder already contains other files: ", "SnapLab のフォルダーにほかのファイルが入っています: " } },
            { "err.venv", new[] { "SnapLab's Python environment could not be prepared. Check free disk space and the internet connection.", "SnapLab の Python の環境を準備できませんでした。ディスクの空きとインターネットの接続を確認してください。" } },
            { "err.open", new[] { "SnapLab was installed but could not start. Open the log for details.", "SnapLab は入りましたが、起動できませんでした。詳しくは記録を見てください。" } },
            { "err.admin", new[] { "The administrator part stopped: ", "管理者の権限で行う手順が止まりました: " } },
            { "uninstall.title", new[] { "Remove SnapLab", "SnapLab を削除" } },
            { "uninstall.body", new[] { "This removes SnapLab, its Linux workspace, and its Start menu entry. Python, Git and WSL stay. Your conversations and settings stay unless you tick the box.",
                                        "SnapLab と、その Linux の作業環境、スタートメニューの項目を削除します。Python、Git、WSL は残します。会話と設定は、下の欄にチェックを入れない限り残します。" } },
            { "uninstall.data", new[] { "Also delete my conversations, settings and saved API keys", "会話、設定、保存した API キーも削除する" } },
            { "uninstall.go", new[] { "Remove", "削除する" } },
            { "uninstall.done", new[] { "SnapLab was removed.", "SnapLab を削除しました。" } },
        };

        public static string Get(string key)
        {
            string[] v;
            if (!S.TryGetValue(key, out v)) return key;
            return Ja ? v[1] : v[0];
        }
    }

    class StepFailed : Exception
    {
        public StepFailed(string message) : base(message) { }
    }

    class Installer
    {
        readonly Options o;
        public const string Denied = "denied";
        public Action<string, string> OnStep;      // (step key, state: running/done/failed/skipped)
        public Func<string, string> RunAdmin;        // runs the administrator part; returns null, "restart", Denied or a message
        public string WorkspaceProblem;
        public bool NeedsRestart;

        public Installer(Options options) { o = options; }

        void Mark(string key, string state) { Log.Write("[step] " + key + " " + state); if (OnStep != null) OnStep(key, state); }

        public static List<string> Missing(Options o)
        {
            var need = new List<string>();
            if (!o.SkipWsl && !Shell.WslWorks()) need.Add("wsl");
            if (Shell.FindPython() == null) need.Add("python");
            if (Shell.FindGit() == null) need.Add("git");
            return need;
        }

        public void Run()
        {
            Mark("check", "running");
            var v = Environment.OSVersion.Version;
            if (!Environment.Is64BitOperatingSystem || v.Major < 10 || (v.Major == 10 && v.Build < 19041)) { Mark("check", "failed"); throw new StepFailed(T.Get("err.os")); }
            Directory.CreateDirectory(o.Home);
            KeepSetupCopy();
            var need = Missing(o);
            Log.Write("missing: " + (need.Count == 0 ? "nothing" : string.Join(",", need)));
            Mark("check", "done");

            if (need.Count > 0)
            {
                Mark("admin", "running");
                string result = RunAdmin(string.Join(",", need));
                Shell.RefreshPath();
                if (result == "restart") { NeedsRestart = true; Mark("admin", "done"); return; }
                if (result != null) { Mark("admin", "failed"); throw new StepFailed(result); }
                Mark("admin", "done");
            }
            else Mark("admin", "skipped");

            Mark("download", "running");
            string git = Shell.FindGit();
            if (git == null) { Mark("download", "failed"); throw new StepFailed(T.Get("err.git")); }
            if (Directory.Exists(Path.Combine(o.AppDir, ".git")))
            {
                // An earlier installation: update it when possible, otherwise keep it as it is.
                Shell.Run(git, "-C " + Shell.Quote(o.AppDir) + " pull --ff-only", null, 600000, null, null);
            }
            else
            {
                if (Directory.Exists(o.AppDir) && Directory.EnumerateFileSystemEntries(o.AppDir).Any())
                { Mark("download", "failed"); throw new StepFailed(T.Get("err.folder") + o.AppDir); }
                Directory.CreateDirectory(Path.GetDirectoryName(o.AppDir));
                if (Shell.Run(git, "-c core.autocrlf=false clone --branch " + o.Branch + " -- " + Shell.Quote(o.Repo) + " " + Shell.Quote(o.AppDir), null, 1800000, null, null) != 0)
                { Mark("download", "failed"); throw new StepFailed(T.Get("err.download")); }
            }
            Mark("download", "done");

            Mark("prepare", "running");
            string python = Shell.FindPython();
            if (python == null) { Mark("prepare", "failed"); throw new StepFailed(T.Get("err.python")); }
            if (!File.Exists(o.VenvPython) && Shell.Run(python, "-m venv " + Shell.Quote(o.Venv), o.AppDir, 600000, null, null) != 0)
            { Mark("prepare", "failed"); throw new StepFailed(T.Get("err.venv")); }
            // Pillow turns photos upright and removes camera metadata (such as the place) before a model sees them.
            if (Shell.Run(o.VenvPython, "-c \"import cryptography, PIL\"", o.AppDir, 120000, null, null) != 0
                && Shell.Run(o.VenvPython, "-m pip install --disable-pip-version-check cryptography pillow", o.AppDir, 1800000, null, null) != 0)
            { Mark("prepare", "failed"); throw new StepFailed(T.Get("err.venv")); }
            Mark("prepare", "done");

            if (o.SkipWsl) { Mark("workspace", "skipped"); WorkspaceProblem = "skipped (--skip-wsl)"; }
            else
            {
                Mark("workspace", "running");
                var output = new StringBuilder();
                int code = Shell.Run(o.VenvPython, "-u " + Shell.Quote(Path.Combine(o.AppDir, "tools", "setup_wsl.py")) + " --resume", o.AppDir, 5400000, output, null);
                if (code == 0) Mark("workspace", "done");
                else
                {
                    string[] lines = output.ToString().Trim().Split('\n');
                    WorkspaceProblem = lines.Length > 0 ? lines[lines.Length - 1].Trim() : ("exit " + code);
                    if (WorkspaceProblem.Length > 300) WorkspaceProblem = WorkspaceProblem.Substring(0, 300);
                    Mark("workspace", "failed");
                }
            }

            Mark("shortcut", "running");
            AddShortcutsAndEntries();
            Mark("shortcut", "done");

            Mark("open", "running");
            string args = Shell.Quote(Path.Combine(o.AppDir, "tools", "launch.py")) + " --data " + Shell.Quote(o.DataDir) + " --port " + o.Port + (o.NoLaunch ? " --no-browser" : "");
            if (Shell.Run(o.VenvPython, args, o.AppDir, 300000, null, null) != 0) { Mark("open", "failed"); throw new StepFailed(T.Get("err.open")); }
            Mark("open", "done");
            ClearResume();
        }

        void KeepSetupCopy()
        {
            try
            {
                string self = Assembly.GetExecutingAssembly().Location;
                if (!string.Equals(Path.GetFullPath(self), Path.GetFullPath(o.KeptExe), StringComparison.OrdinalIgnoreCase))
                    File.Copy(self, o.KeptExe, true);
            }
            catch (Exception ex) { Log.Write("could not keep a copy of setup: " + ex.Message); }
        }

        string LaunchCommand(bool browser)
        {
            return Shell.Quote(o.VenvPythonW) + " " + Shell.Quote(Path.Combine(o.AppDir, "tools", "launch.py")) + " --data " + Shell.Quote(o.DataDir)
                + " --port " + o.Port + (browser ? "" : " --no-browser");
        }

        void AddShortcutsAndEntries()
        {
            string icon = Path.Combine(o.AppDir, "assets", "snaplab.ico");
            if (!o.NoShortcut)
            {
                try
                {
                    string lnk = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), o.Name + ".lnk");
                    Type t = Type.GetTypeFromProgID("WScript.Shell");
                    object shell = Activator.CreateInstance(t);
                    object sc = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { lnk });
                    Type st = sc.GetType();
                    st.InvokeMember("TargetPath", BindingFlags.SetProperty, null, sc, new object[] { o.VenvPythonW });
                    st.InvokeMember("Arguments", BindingFlags.SetProperty, null, sc, new object[] { Shell.Quote(Path.Combine(o.AppDir, "tools", "launch.py")) + " --data " + Shell.Quote(o.DataDir) + " --port " + o.Port });
                    st.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, sc, new object[] { o.AppDir });
                    st.InvokeMember("IconLocation", BindingFlags.SetProperty, null, sc, new object[] { icon + ",0" });
                    st.InvokeMember("Description", BindingFlags.SetProperty, null, sc, new object[] { "Open SnapLab" });
                    st.InvokeMember("Save", BindingFlags.InvokeMethod, null, sc, null);
                    Log.Write("shortcut: " + lnk);
                }
                catch (Exception ex) { Log.Write("shortcut failed: " + ex.Message); }
                using (var k = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + o.Name))
                {
                    k.SetValue("DisplayName", o.Name);
                    k.SetValue("DisplayIcon", icon);
                    k.SetValue("Publisher", "So-Zes");
                    k.SetValue("InstallLocation", o.AppDir);
                    k.SetValue("URLInfoAbout", "https://github.com/So-Zes/snaplab");
                    k.SetValue("UninstallString", Shell.Quote(o.KeptExe) + " --uninstall --name " + Shell.Quote(o.Name)
                        + " --app-dir " + Shell.Quote(o.AppDir) + " --data-dir " + Shell.Quote(o.DataDir));
                    k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                    k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                }
            }
            if (!o.NoStartup)
                using (var k = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run"))
                    k.SetValue(o.Name, LaunchCommand(false));
        }

        public void SetResume()
        {
            KeepSetupCopy();
            using (var k = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\RunOnce"))
                k.SetValue(o.Name + "Setup", Shell.Quote(o.KeptExe) + " --resume --name " + Shell.Quote(o.Name)
                    + " --app-dir " + Shell.Quote(o.AppDir) + " --data-dir " + Shell.Quote(o.DataDir) + " --port " + o.Port);
        }

        void ClearResume()
        {
            using (var k = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\RunOnce"))
                k.DeleteValue(o.Name + "Setup", false);
        }

        public string OpenCommand() { return LaunchCommand(true); }
    }

    // The administrator part: runs in a separate, elevated copy (or in-process with --no-elevate on a machine where
    // this program is already an administrator, such as a test runner). Reports progress as lines in a state file.
    static class AdminPart
    {
        // Runs the part in this process and turns its outcome into what Installer.RunAdmin returns.
        public static string RunHere(Options o, string need)
        {
            o.Need = need;
            o.StateFile = Path.Combine(o.Home, "setup-admin-" + Process.GetCurrentProcess().Id + ".txt");
            File.WriteAllText(o.StateFile, "", Encoding.UTF8);
            int code = Run(o);
            return Outcome(o.StateFile, code);
        }

        // Exit code 0 = done, 3 = restart needed, anything else = stopped (the last state line says why).
        public static string Outcome(string stateFile, int code)
        {
            string text = "";
            try { text = File.ReadAllText(stateFile, Encoding.UTF8); File.Delete(stateFile); } catch (Exception) { }
            Log.Write("[admin result] exit " + code + ": " + text.Trim().Replace("\n", " | "));
            if (code == 0) return null;
            if (code == 3) return "restart";
            string[] lines = text.Trim().Split('\n');
            string last = lines.Length > 0 ? lines[lines.Length - 1].Trim() : "";
            return T.Get("err.admin") + (last.StartsWith("failed ") ? last.Substring(7) : "exit " + code);
        }

        public static int Run(Options o)
        {
            var need = new HashSet<string>((o.Need ?? "").Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries));
            Action<string> state = s => { Log.Write("[admin] " + s); if (o.StateFile != null) File.AppendAllText(o.StateFile, s + "\n", Encoding.UTF8); };
            try
            {
                ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072;  // TLS 1.2
                string winget = Shell.Which("winget.exe");
                if (need.Contains("python"))
                {
                    state("step python");
                    bool ok = winget != null && Shell.Run(winget, "install --id Python.Python.3.13 --exact --source winget --scope machine --silent --accept-package-agreements --accept-source-agreements --disable-interactivity", null, 1800000, null, null) == 0;
                    if (!ok) ok = InstallDownloaded("https://www.python.org/ftp/python/3.13.3/python-3.13.3-amd64.exe", "Python Software Foundation",
                                                    "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_launcher=1");
                    Shell.RefreshPath();
                    if (!ok || Shell.FindPython() == null) { state("failed " + T.Get("err.python")); return 1; }
                }
                if (need.Contains("git"))
                {
                    state("step git");
                    bool ok = winget != null && Shell.Run(winget, "install --id Git.Git --exact --source winget --scope machine --silent --accept-package-agreements --accept-source-agreements --disable-interactivity", null, 1800000, null, null) == 0;
                    if (!ok) ok = InstallDownloaded("https://github.com/git-for-windows/git/releases/download/v2.49.0.windows.1/Git-2.49.0-64-bit.exe", "Johannes Schindelin",
                                                    "/VERYSILENT /NORESTART /NOCANCEL /SP- /SUPPRESSMSGBOXES");
                    Shell.RefreshPath();
                    if (!ok || Shell.FindGit() == null) { state("failed " + T.Get("err.git")); return 1; }
                }
                if (need.Contains("wsl"))
                {
                    state("step wsl");
                    int code = Shell.Run("wsl.exe", "--install --no-distribution", null, 1800000, null, Encoding.Unicode);
                    if (code != 0 && code != 3010) { state("failed " + T.Get("err.wsl")); return 1; }
                    // Turning WSL on for the first time needs a restart before Linux can start, even when
                    // "wsl --version" already answers; setup continues after the next sign-in.
                    state("restart");
                    return 3;
                }
                state("done");
                return 0;
            }
            catch (Exception ex)
            {
                state("failed " + ex.Message);
                return 1;
            }
        }

        static bool InstallDownloaded(string url, string signer, string args)
        {
            string file = Path.Combine(Path.GetTempPath(), "snaplab-" + Path.GetFileName(url));
            try
            {
                Log.Write("download " + url);
                using (var web = new WebClient()) web.DownloadFile(url, file);
                // Only run an installer that carries a valid signature from the expected publisher.
                var sb = new StringBuilder();
                string ps = "$s=Get-AuthenticodeSignature -LiteralPath '" + file.Replace("'", "''") + "'; $s.Status.ToString(); $s.SignerCertificate.Subject";
                Shell.Run("powershell.exe", "-NoProfile -NonInteractive -Command \"" + ps + "\"", null, 120000, sb, null);
                string result = sb.ToString();
                if (!result.StartsWith("Valid") || result.IndexOf(signer, StringComparison.OrdinalIgnoreCase) < 0)
                { Log.Write("signature check failed: " + result.Trim()); return false; }
                return Shell.Run(file, args, null, 1800000, null, null) == 0;
            }
            catch (Exception ex) { Log.Write("download failed: " + ex.Message); return false; }
            finally { try { File.Delete(file); } catch (Exception) { } }
        }
    }

    static class Remover
    {
        public static void Run(Options o, Action<string> say)
        {
            // Stop the running SnapLab from this installation (its Python processes run from the app folder).
            Shell.Run("powershell.exe", "-NoProfile -NonInteractive -Command \"Get-CimInstance Win32_Process -Filter \\\"Name LIKE 'python%'\\\" | Where-Object { $_.CommandLine -like '*" + o.AppDir.Replace("'", "''") + "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }\"", null, 120000, null, null);
            if (o.DeleteData && File.Exists(o.VenvPython))
            {
                // Saved API keys are named after the data folder; remove only this installation's keys.
                var sb = new StringBuilder();
                Shell.Run(o.VenvPython, "-c \"import hashlib,pathlib,sys; print('SNAPLAB_' + hashlib.sha256(str(pathlib.Path(sys.argv[1]).resolve()).encode()).hexdigest()[:16] + '_')\" " + Shell.Quote(o.DataDir), o.AppDir, 60000, sb, null);
                string prefix = sb.ToString().Trim();
                if (prefix.StartsWith("SNAPLAB_") && prefix.Length > 20)
                {
                    var list = new StringBuilder();
                    Shell.Run("cmdkey.exe", "/list", null, 60000, list, Encoding.Default);
                    foreach (string line in list.ToString().Split('\n'))
                    {
                        int at = line.IndexOf(prefix, StringComparison.Ordinal);
                        if (at < 0) continue;
                        string target = line.Substring(at).Trim();
                        Shell.Run("cmdkey.exe", "/delete:" + target, null, 60000, null, Encoding.Default);
                    }
                }
            }
            // The Linux workspace: only one that SnapLab created (it left setup-result.json).
            string wslDir = Environment.GetEnvironmentVariable("SNAPLAB_WSL_DIR");
            if (string.IsNullOrEmpty(wslDir)) wslDir = Path.Combine(o.Home, "wsl");
            string distro = Environment.GetEnvironmentVariable("SNAPLAB_WSL_DISTRO");
            if (string.IsNullOrEmpty(distro)) distro = "SnapLab-Worker";
            if (File.Exists(Path.Combine(wslDir, "setup-result.json")))
            {
                say(distro);
                Shell.Run("wsl.exe", "--terminate " + distro, null, 120000, null, Encoding.Unicode);
                Shell.Run("wsl.exe", "--unregister " + distro, null, 600000, null, Encoding.Unicode);
                TryDelete(wslDir);
            }
            using (var k = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run")) k.DeleteValue(o.Name, false);
            using (var k = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\RunOnce")) k.DeleteValue(o.Name + "Setup", false);
            try { File.Delete(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), o.Name + ".lnk")); } catch (Exception) { }
            Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + o.Name, false);
            say(o.AppDir);
            TryDelete(o.AppDir);
            if (o.DeleteData) TryDelete(o.DataDir);
        }

        static void TryDelete(string dir)
        {
            if (!Directory.Exists(dir)) return;
            try
            {
                foreach (string f in Directory.EnumerateFiles(dir, "*", SearchOption.AllDirectories))
                    try { File.SetAttributes(f, FileAttributes.Normal); } catch (Exception) { }
                Directory.Delete(dir, true);
                Log.Write("removed " + dir);
            }
            catch (Exception ex) { Log.Write("could not remove " + dir + ": " + ex.Message); }
        }
    }

    class SetupForm : Form
    {
        static readonly Color Ink = Color.FromArgb(22, 23, 27), Muted = Color.FromArgb(91, 95, 104), Brand = Color.FromArgb(200, 22, 40),
            Good = Color.FromArgb(12, 124, 93), Bad = Color.FromArgb(181, 16, 31);
        readonly Options o;
        readonly Panel page = new Panel();
        readonly FlowLayoutPanel buttons = new FlowLayoutPanel();
        readonly Dictionary<string, Label[]> rows = new Dictionary<string, Label[]>();
        Label detail, elapsed;
        DateTime stepStarted;
        readonly System.Windows.Forms.Timer clock = new System.Windows.Forms.Timer();
        bool busy;
        int shot;
        Font body, heading, small;

        public SetupForm(Options options)
        {
            o = options;
            Text = T.Get("title");
            AutoScaleMode = AutoScaleMode.Dpi;
            AutoScaleDimensions = new SizeF(96F, 96F);
            string face = System.Globalization.CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "ja" ? "Yu Gothic UI" : "Segoe UI";
            body = new Font(face, 10F);
            heading = new Font(face, 16F, FontStyle.Bold);
            small = new Font(face, 9F);
            Font = body;
            ClientSize = new Size(620, 520);
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.White;
            try { Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location); } catch (Exception) { }
            buttons.FlowDirection = FlowDirection.RightToLeft;
            buttons.Dock = DockStyle.Bottom;
            buttons.Height = 56;
            buttons.Padding = new Padding(16, 10, 16, 10);
            buttons.BackColor = Color.FromArgb(247, 243, 240);
            page.Dock = DockStyle.Fill;
            page.Padding = new Padding(28, 24, 28, 8);
            page.AutoScroll = true;
            Controls.Add(page);
            Controls.Add(buttons);
            FormClosing += (s, e) =>
            {
                if (busy && e.CloseReason == CloseReason.UserClosing &&
                    MessageBox.Show(this, T.Get("stop.ask"), T.Get("title"), MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                    e.Cancel = true;
            };
            Shown += (s, e) => { if (o.Uninstall) ShowUninstall(); else ShowWelcome(null); };
            clock.Interval = 1000;
            clock.Tick += (s, e) =>
            {
                if (elapsed == null || elapsed.IsDisposed) return;
                var span = DateTime.Now - stepStarted;
                elapsed.Text = ((int)span.TotalMinutes).ToString() + ":" + span.Seconds.ToString("00");
            };
        }

        FlowLayoutPanel NewPage(string title)
        {
            page.Controls.Clear();
            buttons.Controls.Clear();
            var flow = new FlowLayoutPanel();
            flow.FlowDirection = FlowDirection.TopDown;
            flow.WrapContents = false;
            flow.AutoSize = true;
            flow.Location = new Point(page.Padding.Left, page.Padding.Top);
            page.Controls.Add(flow);
            var head = new FlowLayoutPanel();
            head.AutoSize = true;
            head.WrapContents = false;
            head.Margin = new Padding(0, 0, 0, 14);
            var logo = new PictureBox();
            logo.Size = new Size(44, 44);
            logo.SizeMode = PictureBoxSizeMode.Zoom;
            try { logo.Image = Image.FromStream(Assembly.GetExecutingAssembly().GetManifestResourceStream("logo.png")); } catch (Exception) { }
            head.Controls.Add(logo);
            var t = new Label();
            t.Text = title;
            t.Font = heading;
            t.ForeColor = Ink;
            t.AutoSize = true;
            t.Margin = new Padding(10, 6, 0, 0);
            head.Controls.Add(t);
            flow.Controls.Add(head);
            return flow;
        }

        Label Para(FlowLayoutPanel flow, string text, Font font, Color color, int top)
        {
            var l = new Label();
            l.Text = text;
            l.Font = font;
            l.ForeColor = color;
            l.AutoSize = true;
            l.MaximumSize = new Size(ClientSize.Width - page.Padding.Horizontal - 24, 0);
            l.Margin = new Padding(0, top, 0, 2);
            flow.Controls.Add(l);
            return l;
        }

        Button AddButton(string text, bool primary, EventHandler click)
        {
            var b = new Button();
            b.Text = text;
            b.AutoSize = true;
            b.MinimumSize = new Size(110, 34);
            b.Margin = new Padding(8, 0, 0, 0);
            b.FlatStyle = FlatStyle.Flat;
            b.Font = body;
            if (primary) { b.BackColor = Brand; b.ForeColor = Color.White; b.FlatAppearance.BorderColor = Brand; }
            else { b.BackColor = Color.White; b.ForeColor = Ink; b.FlatAppearance.BorderColor = Color.FromArgb(214, 206, 200); }
            b.Click += click;
            buttons.Controls.Add(b);
            if (primary) AcceptButton = b;
            return b;
        }

        void Shot(string name)
        {
            if (o.Screenshots == null) return;
            Application.DoEvents();
            Directory.CreateDirectory(o.Screenshots);
            using (var bmp = new Bitmap(Width, Height))
            {
                DrawToBitmap(bmp, new Rectangle(0, 0, Width, Height));
                bmp.Save(Path.Combine(o.Screenshots, (++shot).ToString("00") + "-" + name + ".png"), ImageFormat.Png);
            }
        }

        void ShowWelcome(string notice)
        {
            busy = false;
            var flow = NewPage(T.Get(o.Resume ? "resume" : "welcome"));
            Para(flow, T.Get("intro"), body, Ink, 0);
            Para(flow, T.Get("what"), new Font(body, FontStyle.Bold), Ink, 14);
            foreach (string k in new[] { "what1", "what2", "what3" }) Para(flow, "•  " + T.Get(k), body, Ink, 2);
            Para(flow, T.Get("why"), new Font(body, FontStyle.Bold), Ink, 14);
            Para(flow, T.Get("why1"), body, Ink, 2);
            Para(flow, T.Get("time"), small, Muted, 14);
            if (notice != null) Para(flow, notice, body, Bad, 12);
            AddButton(T.Get(o.Resume ? "continue" : "install"), true, (s, e) => StartInstall());
            AddButton(T.Get("cancel"), false, (s, e) => Close());
            Shot("welcome");
            if (o.Auto) BeginInvoke(new Action(StartInstall));
        }

        void ShowProgress()
        {
            busy = true;
            var flow = NewPage(T.Get("installing"));
            rows.Clear();
            foreach (string key in new[] { "check", "admin", "download", "prepare", "workspace", "shortcut", "open" })
            {
                var line = new FlowLayoutPanel();
                line.AutoSize = true;
                line.WrapContents = false;
                line.Margin = new Padding(0, 4, 0, 0);
                var glyph = new Label();
                glyph.Text = "○";
                glyph.Font = new Font("Segoe UI Symbol", 10F);
                glyph.ForeColor = Muted;
                glyph.Size = new Size(26, 22);
                var text = new Label();
                text.Text = T.Get("s." + key);
                text.AutoSize = true;
                text.ForeColor = Muted;
                line.Controls.Add(glyph);
                line.Controls.Add(text);
                flow.Controls.Add(line);
                rows[key] = new[] { glyph, text };
            }
            var bar = new ProgressBar();
            bar.Style = ProgressBarStyle.Marquee;
            bar.MarqueeAnimationSpeed = 30;
            bar.Size = new Size(ClientSize.Width - page.Padding.Horizontal - 24, 8);
            bar.Margin = new Padding(0, 18, 0, 6);
            flow.Controls.Add(bar);
            elapsed = Para(flow, "", small, Muted, 0);
            detail = Para(flow, "", small, Muted, 0);
            detail.AutoSize = false;
            detail.Size = new Size(ClientSize.Width - page.Padding.Horizontal - 24, 40);
            var link = new LinkLabel();
            link.Text = T.Get("details");
            link.AutoSize = true;
            link.Font = small;
            link.Margin = new Padding(0, 4, 0, 0);
            link.LinkClicked += (s, e) => { try { Process.Start("notepad.exe", Shell.Quote(Log.FilePath)); } catch (Exception) { } };
            flow.Controls.Add(link);
        }

        void SetStep(string key, string state)
        {
            if (InvokeRequired) { BeginInvoke(new Action(() => SetStep(key, state))); return; }
            Label[] r;
            if (!rows.TryGetValue(key, out r)) return;
            if (state == "running") { stepStarted = DateTime.Now; clock.Start(); r[0].Text = "➜"; r[0].ForeColor = Brand; r[1].ForeColor = Ink; r[1].Font = new Font(body, FontStyle.Bold); }
            else if (state == "done") { r[0].Text = "✔"; r[0].ForeColor = Good; r[1].ForeColor = Ink; r[1].Font = body; }
            else if (state == "failed") { r[0].Text = "✖"; r[0].ForeColor = Bad; r[1].ForeColor = Bad; r[1].Font = body; }
            else { r[0].Text = "–"; r[0].ForeColor = Muted; r[1].ForeColor = Muted; r[1].Font = body; }
            if (state == "running" || state == "failed") Shot("step-" + key + "-" + state);
        }

        void SetDetail(string line)
        {
            if (detail == null) return;
            if (InvokeRequired) { BeginInvoke(new Action(() => SetDetail(line))); return; }
            string t = line.Trim();
            if (t.StartsWith("[step]") || t.StartsWith(">") || t.StartsWith("exit ") || t.StartsWith("stopped after")) return;
            if (t.Length > 160) t = t.Substring(0, 160) + "...";
            detail.Text = t;
        }

        void StartInstall()
        {
            ShowProgress();
            Log.Listener = SetDetail;
            var installer = new Installer(o);
            installer.OnStep = SetStep;
            installer.RunAdmin = RunAdminPart;
            var thread = new Thread(() =>
            {
                string failure = null;
                try { installer.Run(); }
                catch (StepFailed ex) { failure = ex.Message; }
                catch (Exception ex) { failure = ex.Message; Log.Write(ex.ToString()); }
                BeginInvoke(new Action(() =>
                {
                    Log.Listener = null;
                    busy = false;
                    if (failure == Installer.Denied) ShowWelcome(T.Get("denied"));
                    else if (failure != null) ShowFailed(failure);
                    else if (installer.NeedsRestart) ShowRestart(installer);
                    else ShowDone(installer);
                }));
            });
            thread.IsBackground = true;
            thread.Start();
        }

        string RunAdminPart(string need)
        {
            if (o.NoElevate) return AdminPart.RunHere(o, need);
            string stateFile = Path.Combine(o.Home, "setup-admin-" + Process.GetCurrentProcess().Id + ".txt");
            File.WriteAllText(stateFile, "", Encoding.UTF8);
            SetDetail(T.Get("waitadmin"));
            var psi = new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,
                "--admin-steps " + Shell.Quote(stateFile) + " --need " + need + " --profile " + Shell.Quote(o.Profile) + " --name " + Shell.Quote(o.Name));
            psi.UseShellExecute = true;
            psi.Verb = "runas";
            Process p;
            try { p = Process.Start(psi); }
            catch (System.ComponentModel.Win32Exception) { Log.Write("administrator permission was not given"); return Installer.Denied; }
            p.WaitForExit();
            return AdminPart.Outcome(stateFile, p.ExitCode);
        }

        void ShowRestart(Installer installer)
        {
            installer.SetResume();
            var flow = NewPage(T.Get("restart.title"));
            Para(flow, T.Get("restart.body"), body, Ink, 0);
            AddButton(T.Get("restart.now"), true, (s, e) =>
            {
                Process.Start(new ProcessStartInfo("shutdown.exe", "/r /t 0") { CreateNoWindow = true, UseShellExecute = false });
                Close();
            });
            AddButton(T.Get("restart.later"), false, (s, e) => Close());
            Shot("restart");
            if (o.Auto) BeginInvoke(new Action(Close));
        }

        void ShowDone(Installer installer)
        {
            var flow = NewPage(T.Get("done.title"));
            if (!o.NoLaunch) Para(flow, T.Get("done.body"), body, Ink, 0);
            Para(flow, T.Get("done.pc"), body, Ink, o.NoLaunch ? 0 : 12);
            Para(flow, T.Get("done.again"), small, Muted, 12);
            if (installer.WorkspaceProblem != null) Para(flow, T.Get("done.noworkspace") + installer.WorkspaceProblem, body, Bad, 14);
            AddButton(T.Get("close"), true, (s, e) => Close());
            AddButton(T.Get("done.open"), false, (s, e) => ThreadPool.QueueUserWorkItem(_ =>
                Shell.Run(o.VenvPython, Shell.Quote(Path.Combine(o.AppDir, "tools", "launch.py")) + " --data " + Shell.Quote(o.DataDir) + " --port " + o.Port, o.AppDir, 300000, null, null)));
            Shot("done");
            if (o.Auto) BeginInvoke(new Action(Close));
        }

        void ShowFailed(string message)
        {
            var flow = NewPage(T.Get("fail.title"));
            Para(flow, message, new Font(body, FontStyle.Bold), Bad, 0);
            Para(flow, T.Get("fail.body"), body, Ink, 12);
            var link = new LinkLabel();
            link.Text = Log.FilePath;
            link.AutoSize = true;
            link.Font = small;
            link.Margin = new Padding(0, 12, 0, 0);
            link.LinkClicked += (s, e) => { try { Process.Start("notepad.exe", Shell.Quote(Log.FilePath)); } catch (Exception) { } };
            flow.Controls.Add(link);
            AddButton(T.Get("tryagain"), true, (s, e) => StartInstall());
            AddButton(T.Get("close"), false, (s, e) => Close());
            Shot("failed");
            if (o.Auto) BeginInvoke(new Action(Close));
        }

        void ShowUninstall()
        {
            var flow = NewPage(T.Get("uninstall.title"));
            Para(flow, T.Get("uninstall.body"), body, Ink, 0);
            var box = new CheckBox();
            box.Text = T.Get("uninstall.data");
            box.AutoSize = true;
            box.Margin = new Padding(0, 16, 0, 0);
            box.Checked = o.DeleteData;
            flow.Controls.Add(box);
            AddButton(T.Get("uninstall.go"), true, (s, e) =>
            {
                o.DeleteData = box.Checked;
                busy = true;
                foreach (Control c in buttons.Controls) c.Enabled = false;
                var status = Para(flow, "...", small, Muted, 14);
                var thread = new Thread(() =>
                {
                    Remover.Run(o, what => BeginInvoke(new Action(() => status.Text = what)));
                    BeginInvoke(new Action(() =>
                    {
                        busy = false;
                        var done = NewPage(T.Get("uninstall.title"));
                        Para(done, T.Get("uninstall.done"), body, Ink, 0);
                        AddButton(T.Get("close"), true, (s2, e2) => Close());
                        Shot("removed");
                    }));
                });
                thread.IsBackground = true;
                thread.Start();
            });
            AddButton(T.Get("cancel"), false, (s, e) => Close());
            Shot("uninstall");
        }
    }

    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            Options o;
            try { o = Options.Parse(args); }
            catch (Exception ex) { MessageBox.Show(ex.Message, "SnapLab Setup"); return 2; }
            Directory.CreateDirectory(o.Home);
            Log.FilePath = Path.Combine(o.Home, o.Name == "SnapLab" ? "install.log" : "install-" + o.Name + ".log");
            Log.Write("SnapLab Setup " + Assembly.GetExecutingAssembly().GetName().Version + " " + string.Join(" ", args));
            if (o.AdminSteps) return AdminPart.Run(o);
            if (o.Quiet) return Quiet(o);
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var form = new SetupForm(o)) Application.Run(form);
            return 0;
        }

        // No window: for automated checks. Exit code 0 = installed (or removed), 3 = restart needed, 1 = stopped.
        static int Quiet(Options o)
        {
            if (o.Uninstall) { Remover.Run(o, s => Log.Write("removing " + s)); return 0; }
            var installer = new Installer(o);
            installer.RunAdmin = need =>
            {
                if (!o.NoElevate) return "administrator part needed for " + need + " (run without --quiet, or with --no-elevate as administrator)";
                return AdminPart.RunHere(o, need);
            };
            try
            {
                installer.Run();
                if (installer.WorkspaceProblem != null) Log.Write("workspace: " + installer.WorkspaceProblem);
                Log.Write("result: " + (installer.NeedsRestart ? "restart" : "installed"));
                return installer.NeedsRestart ? 3 : 0;
            }
            catch (Exception ex) { Log.Write("result: stopped: " + ex.Message); return 1; }
        }
    }
}
