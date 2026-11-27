# Hardware support and validation

Exact model capabilities, manufacturer references, and recorded hardware tests for
rigol-mcp. For installation, connection options, and tool usage, see the
[README](../README.md).

## Model capabilities and channel selection

The server looks up the model field from `*IDN?` in the packaged
[`models.toml`](../src/rigol_mcp/models.toml) capability catalog. Python loads and
validates this catalog once; it ships with the tool and is versioned with the code.
To add a model, add its exact uppercase name, family, and positive integer
`analog_channel_count` to `[models]`, with a manufacturer source below. Optional
`[aliases]` entries must point directly to canonical model names. A new family still
needs a driver for dialect-dependent tools.

State reads query only that model's analog channels. Channel operations reject
unavailable inputs before sending channel commands, including before enabling either
source of a two-channel measurement. The `idn` diagnostic reports the model and its
available analog channels. Identification is cached for the connection and reset on
disconnect or retry cleanup.

Tool schemas list the union of known channel names without contacting the scope;
runtime validation checks the connected model. Unknown models are rejected rather
than assigned a channel count from their model-number digits. Model names are
case-insensitive, with only explicitly listed spelling aliases accepted.

## Manufacturer references

Manufacturer links below use Rigol-owned domains. Current model tables substantiate
the listed channel counts; legacy entries with incomplete exact-model citations are
marked separately. Counts describe analog inputs; digital inputs and signal-generator
outputs are separate. A capability entry does **not** establish full SCPI
compatibility or hardware verification.
DS2000A/MSO2000A entries provide channel information only: those families still need
a dedicated dialect driver for driver-dependent tools such as measurements, waveforms,
screenshots, and acquisition exports.

| Models | Analog channels | Manufacturer source |
|---|---:|---|
| DS1054Z | 4 | [DS1000Z datasheet, models and key specifications](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/DS1000Z_DataSheet_EN.pdf) |
| DS1074Z, DS1074Z-S, DS1104Z, DS1104Z-S (legacy entries; see source limits below) | 4 | [Rigol 1000Z series overview](https://www.rigolna.com/products/digital-oscilloscopes/1000z/) documents the four-channel platform; its current model table lists Plus variants instead |
| MSO1074Z, MSO1074Z-S, MSO1104Z, MSO1104Z-S | 4 | [DS1000Z programming guide, document overview](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DS1000Z_ProgrammingGuide_EN.pdf) |
| DS1074Z Plus, DS1074Z-S Plus, DS1104Z Plus, DS1104Z-S Plus | 4 | [DS1000Z datasheet](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/DS1000Z_DataSheet_EN.pdf) |
| DHO802, DHO812 | 2 | [DHO800 product table](https://www.rigolna.com/products/digital-oscilloscopes/dho800/) |
| DHO804, DHO814, DHO824 | 4 | [DHO800 user guide, document overview](https://www.rigol.com/dam/global/downloads/brochures/zh/user-manual/oscillosopes/DHO800_UserGuide_CN.pdf) |
| DHO914, DHO914S, DHO924, DHO924S | 4 | [DHO900 product table](https://www.rigolna.com/products/digital-oscilloscopes/dho900/) |
| DHO1072, DHO1102, DHO1202 | 2 | [DHO1000/4000 programming guide, document overview](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DHO10004000_ProgrammingGuide_EN.pdf) |
| DHO1202U / DHO1204U | 2 / 4 | [DHO1000U user guide, p.10](https://download.rigol.com/en/Manual/Digital%20Oscilloscope/DHO1000/DHO1000U_UserGuide_EN.pdf) |
| DHO1074, DHO1104, DHO1204, DHO4204, DHO4404, DHO4804 | 4 | [DHO1000/4000 programming guide, document overview](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DHO10004000_ProgrammingGuide_EN.pdf) |
| DS2102A, DS2202A, DS2302A, MSO2102A, MSO2102A-S, MSO2202A, MSO2202A-S, MSO2302A, MSO2302A-S | 2 | [Rigol MSO/DS2000A datasheet, models and ordering information](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MSODS2000A_Datasheet_EN.pdf) |
| DS2072A, DS2072A-S, DS2102A-S, DS2202A-S, DS2302A-S, MSO2072A, MSO2072A-S (legacy entries; see source limits below) | 2 | [Rigol MSO/DS2000A datasheet](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MSODS2000A_Datasheet_EN.pdf) documents the two-channel family, but does not list these exact variants |

**Legacy source limits:** replacing distributor-hosted references exposed gaps in
the current Rigol-hosted model tables. The two legacy rows retain the existing
exact catalog entries and channel counts; the linked family references do not
establish every exact spelling in those rows. Archived Rigol documentation or
exact-model hardware reports are still needed to close those gaps. In particular,
the seven legacy DS2000A/MSO2000A entries above should not be described as verified
published models solely from the current datasheet. DS1104Z has independent hardware
coverage recorded below. This documentation update does not change model matching.

Rigol's [DS1000Z product page](https://www.rigol.com/intl/products/oscilloscope/DS1000Z.html)
also spells the four Plus models with `-Plus`; these are explicit aliases for the
space-separated names above. Other suffixes are not inferred. The earlier README
listed `MSO1054Z`, but it is omitted from this registry because the cited model tables
do not substantiate it.

Additional published models were reviewed on 2026-11-27. These additions have
manufacturer documentation. DHO1000U uses the existing DHO driver because Rigol lists it on the
[DHO1000 product page](https://www.rigol.com/intl/products/oscilloscope/DHO1000.html)
with the shared DHO1000/4000 programming guide. This mapping is not hardware-verified.

The following newer families have **channel metadata only**, pending dialect
validation and driver implementation. A DHO or MHO prefix alone does not select
the existing DHO driver. Digital inputs, external trigger inputs, and generator
outputs are excluded from the analog channel counts.

| Models | Analog channels | Manufacturer source |
|---|---:|---|
| MHO934, MHO954, MHO984 | 4 | [MHO900 product overview and model list](https://www.rigol.com/intl/products/oscilloscope/MHO900.html) |
| MHO98 special edition | 4 | [MHO98 datasheet, p.6](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHO98-DataSheet.pdf) |
| MHO2024, MHO2034 | 4 | [MHO2000 programming guide, p.2](https://download.rigol.com/en/Manual/Digital%20Oscilloscope/MHO2000/MHO2000_Programming%20Guide_EN.pdf) |
| DHO5054, DHO5104 | 4 | [MHO/DHO5000 datasheet, p.9](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHODHO5000_DataSheet.pdf) |
| DHO5058, DHO5108 | 8 | [MHO/DHO5000 datasheet, p.9](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHODHO5000_DataSheet.pdf) |
| MHO5054, MHO5104 | 4 | [MHO/DHO5000 datasheet, p.9](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHODHO5000_DataSheet.pdf) |
| MHO5056, MHO5106 | 6 | [MHO/DHO5000 datasheet, p.9](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHODHO5000_DataSheet.pdf) |
| DS9202, MSO9402 | 2 | [MSO/DS9000 user guide, p.11](https://www.rigol.com/dam/global/downloads/brochures/en/user-manual/oscillosopes/MSODS9000_UserGuide_EN.pdf) |
| DS9204, DS9404, DS9604 | 4 | [MSO/DS9000 user guide, p.11](https://www.rigol.com/dam/global/downloads/brochures/en/user-manual/oscillosopes/MSODS9000_UserGuide_EN.pdf) |

Tool schemas consequently advertise CHAN1–CHAN8. Runtime checks still restrict
each call to the connected model's documented inputs; advertising CHAN8 does not
imply an eight-channel SCPI driver is implemented.

## Recorded hardware reports

These reports describe particular revisions and exercised functions, not complete
SCPI coverage or a hardware pass for every later change. Offline tests and published
channel counts are separate from live validation. Reports reviewed on 2026-11-27:

| Model | Report and transport | Recorded coverage and limits |
|---|---|---|
| DS1054Z | Maintainer, USB with WinUSB/pyvisa-py; [PR #1 merge report](https://github.com/erebusnz/rigol-mcp/pull/1#issuecomment-4654467429) | Identification, screen waveform, measurement, and screenshot tested end-to-end after the DHO integration. |
| DS1104Z | Maintainer, USB/pyvisa-py; [PR #8](https://github.com/erebusnz/rigol-mcp/pull/8), earlier [RAW export](https://github.com/erebusnz/rigol-mcp/pull/3) and [recovery/disconnect](https://github.com/erebusnz/rigol-mcp/pull/4) reports | PR #8 exercised identity, four-channel state, acquisition information, invalid-channel rejection, reconnect, and injected-timeout recovery. Earlier reports cover four-channel RAW export and repeated captures. |
| DHO924S | Contributor `irudoy`, LAN, firmware `00.01.05`; [PR #1 retest](https://github.com/erebusnz/rigol-mcp/pull/1#issuecomment-4650396253) | Acquisition controls, channel/timebase/trigger configuration, measurements with AWG loopback, cursors, and screenshots. The retest found the bare-CSV waveform regression that was fixed before integration. USB was not verified. |
| DHO804 | Contributor `4pplet`; [PR #7](https://github.com/erebusnz/rigol-mcp/pull/7) | Reports operating a Raspberry Pi CM5 deployment with Caddy HTTPS and Claude Code on a Mac. The scope connection transport, firmware, and per-tool hardware checklist are unspecified; the listed HTTP tests are offline. Record as reported usage. |
| DS2202A | Contributor report recorded in [PR #5](https://github.com/erebusnz/rigol-mcp/pull/5), USB with NI-VISA | Identification before and after disconnect, predating the revised retry handling. Connection-only evidence; no DS2000A dialect validation or hardware pass for PR #8. |
| DHO814 | Dave Glover's external `gloveboxes/rigol-mcp` fork, Ethernet, firmware `00.01.05`; [article](https://techcommunity.microsoft.com/blog/educatordeveloperblog/connecting-github-copilot-to-my-oscilloscope/4555545) and [2026-09-10 acceptance report](https://github.com/gloveboxes/rigol-mcp/blob/ffce0bc501ddffcf2f99132c0fb19ebf68bd9dc4/docs/reports/dho814_advanced_mcp_acceptance_report.json) | Reports live signal measurements, cursors, protocol decoding, recording/replay, and RAW transfers. This uses an extended fork; it does not validate upstream behavior or add those features here. Upstream DHO814 remains hardware-unverified. |

Models without a report above remain hardware-unverified in this project. The
`models.toml` comments preserve the distinction between hardware tests, reported
usage, connection-only tests, and external-fork evidence.

