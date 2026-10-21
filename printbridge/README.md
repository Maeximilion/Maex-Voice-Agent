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

First a test slip, without a server and without a real order:

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

On Windows, the Task Scheduler starts the bridge at logon ("At startup", "Restart on failure").

The times on the slip (ready at, printed) come from the server in the operation's time zone; the computer's clock plays no role.

With a network printer the bridge queries the status before every slip (offline, paper). For this, the printer's automatic status back (ASB) must be off, which is how it is set from the factory.

Register operation continues: the register and the bridge share the printer, the jobs arrive one after the other.
