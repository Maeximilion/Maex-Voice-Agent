# Print bridge

Fetches kitchen slips from the server and prints them on the receipt printer at the restaurant (T-4.6).

The server sits in the data center and cannot reach the printer at the restaurant. The bridge runs on a computer at the restaurant, asks for new slips every two seconds, prints them and reports back. It only calls outward: no port needs to be open in the router.

What happens when something fails:

| Failure | Consequence |
|---|---|
| Printer off, paper empty, lid open | Bridge reports the error, the card on the tablet turns red at once, "Send again" |
| Bridge or internet at the restaurant gone | After 60 s without a pickup the card turns red (watchdog on the server), alarm |
| Acknowledgement is lost | The slip comes again after 60 s, the bridge recognizes it and prints no second slip |
| Team corrects before the first slip is printed | Only the new state is printed, never an old one after a new one |

## Prerequisites

- Exactly one bridge per operation: the print log lives on its computer, a second bridge could print a slip twice

On Windows the bridge is one program file that needs nothing else, printers included ([Program file on Windows](#program-file-on-windows)). Run from the source instead, it needs:

- Python 3.12 on the computer that runs the bridge
- Network printer (Epson with a network card): no further packages
- Printer connected by USB to the Windows computer: `pip install pywin32`

Which connection? On Windows: Printers and scanners (German: "Drucker und Scanner"), right-click the printer, printer properties, Ports tab (German: "Anschlüsse"). `USB001` means USB, `IP_…` or `EpsonNet…` means network.

## Settings

As environment variables:

| Name | Example | Meaning |
|---|---|---|
| `MAEX_SERVER_URL` | `https://agent.example.com` | Server, https only (except localhost) |
| `MAEX_KITCHEN_TOKEN` | long and random | Same value as `KITCHEN_BRIDGE_TOKEN` on the server; there also `KITCHEN_BRIDGE_TENANT_ID` (required): the token is valid only for this operation |
| `MAEX_TENANT_ID` | UUID | Operation whose slips are printed |
| `MAEX_PRINTER` | `tcp:192.168.1.50` or `windows:EPSON TM-T20II Kitchen` | Printer |
| `MAEX_PRINTER_WIDTH` | `48` | Characters per line (80 mm paper, font A) |
| `MAEX_STATE_FILE` | `C:\maex\printbridge_state.json` | Print log, order ids only |
| `MAEX_INTERVAL_SECONDS` | `2` | Polling interval |

## Commissioning

First a test slip, without a server and without a real order. Commands are written for the source here; the program file takes the same flags and settings (`maex-printbridge.exe --test`).

```bash
python -m printbridge --test
```

The test slip carries a notice block above the slip and again before the cut (centered, double size, white on black) that asks for a photo of the slip. A slip of a real order carries no such block. Checked on paper on 06.10.2026 (`EPSON TM-T20II Receipt`, Windows queue).

Then a single pass and after that continuous operation:

```bash
python -m printbridge --once
```

```bash
python -m printbridge
```

The times on the slip (ready at, printed) come from the server in the operation's time zone; the computer's clock plays no role.

With a network printer the bridge queries the status before every slip (offline, paper). For this, the printer's automatic status back (ASB) must be off, which is how it is set from the factory.

Register operation continues: the register and the bridge share the printer, the jobs arrive one after the other.

## Program file on Windows

`maex-printbridge.exe` is the bridge as one file (T-9.6): no Python and no pywin32 on the register PC, the same `--test`, `--once` and settings as above. `.github/workflows/printbridge.yml` builds it on a Windows runner with the pins of `requirements-build.txt`, which never reach the register PC. `install.ps1` copies it to `%LOCALAPPDATA%\MaexPrintBridge` and registers the task `Maex Print Bridge`: it starts hidden when this Windows user signs in, and `run.ps1` starts the program again 10 s after it ended. No file holds the token: it stays in the setting `MAEX_KITCHEN_TOKEN`.

### Setting it up on the register PC

Sign in to Windows as the user who works at the register.

1. **Get the package.** On GitHub: Actions, workflow `printbridge`, the newest green run on `main`, section Artifacts, `maex-printbridge-<commit>`. Packages are kept for 90 days; with none left, "Run workflow" builds a new one. Unpack it into a folder, for example `Downloads\maex-printbridge`. It holds `maex-printbridge.exe`, `install.ps1` and `run.ps1`.
2. **Remove what came before.** If a bridge started by hand or through an older Task Scheduler entry exists, stop and delete it: exactly one bridge per operation.
3. **Store the settings.** In Command Prompt (`cmd`), not in PowerShell, whose history file would keep the token:

   ```bat
   setx MAEX_SERVER_URL "https://agent.example.com"
   setx MAEX_KITCHEN_TOKEN "<value of KITCHEN_BRIDGE_TOKEN on the server>"
   setx MAEX_TENANT_ID "<KITCHEN_BRIDGE_TENANT_ID of the server>"
   setx MAEX_PRINTER "windows:EPSON TM-T20II Receipt"
   ```

   Each line answers `SUCCESS: Specified value was saved.` The other settings of the table above can stay unset. Close this console: a console only sees the values that existed when it opened.
4. **Print a test slip.** In a new Command Prompt, in the unpacked folder:

   ```bat
   maex-printbridge.exe --test
   ```

   The file is not signed, so Windows can warn once: "More info", then "Run anyway". The test slip comes out at the register. If an antivirus program removes the file, allow this file and start this step again.
5. **Install.** In the same console and folder:

   ```bat
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

   It ends with `Print bridge installed in ...`. It asks for no admin rights, but no account without them has tried it yet. On "Access denied", open Command Prompt with "Run as administrator" for this same user and repeat the line.
6. **Read the log.**

   ```bat
   type %LOCALAPPDATA%\MaexPrintBridge\bridge.log
   ```

   Good: `Druckbruecke gestartet` and no `Durchlauf abgebrochen` after it. The header of the tablet shows no red banner.
7. **Check the autostart.** Sign out of Windows and sign in again. No window stays on the screen; one that does would stop the bridge when somebody closes it, so report it. Repeat step 6: the time of `Druckbruecke gestartet` is the time of this sign-in.
8. **Check the restart.** In Task Manager, Details, end one `maex-printbridge.exe`. After about 15 s step 6 shows a new start.
9. **Print a real slip.** Place a test order. The input slip comes out at the register and the card on the tablet stays normal.

**Update:** unpack the new package and repeat step 5. The print log stays.

**Remove:** in the unpacked folder, `powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall`. This deletes the task, the folder with print log and log, and the settings of this user with the token. A print log kept elsewhere through `MAEX_STATE_FILE` stays.

| In the log | Meaning |
|---|---|
| `Einstellung fehlt` | Setting not stored or not yet seen: step 3, then sign out and in |
| `nicht gefunden` | Name after `windows:` differs from the queue in "Drucker und Scanner" |
| `CERTIFICATE_VERIFY_FAILED` | Windows lacks the root certificate: open the server address once in Edge, then sign out and in |

The bridge runs only while this user is signed in. A PC at the sign-in screen or asleep prints nothing; the server notices that as a missing bridge (the table at the top).

### What has been run

**On a GitHub Windows runner, at every build** (`windows-latest`, Windows PowerShell 5.1, an account with admin rights; `scripts/printbridge_smoke.py`, `scripts/printbridge_install_check.ps1`): the built program answers `--help`, reports a missing server through `--once` with exit code 1, gets through https with its certificate check to a public server, reaches `win32print` for an unknown queue, and sends the bytes of the test slip in PC858 to a listener on TCP. The installer refuses to run without settings, registers the task (logon trigger, no time limit, install folder, no token in it), and the task started by command runs the program hidden and with a log. After a kill the program comes back. A refused update leaves the running bridge alone, and an update over it keeps the print log. Removing it leaves no task, folder, program or token.

**Nobody has run, and only the register PC can show:** a real sign-in firing the trigger (the runner starts the task by command), an account without admin rights, whether a window stays visible at sign-in (the runner has no screen), the warning of Windows or an antivirus program about the unsigned file, a slip through a real Windows queue from the program file (the source did that on paper on 06.10.2026), and the bridge against our real server. Steps 3 to 9 above cover these.
