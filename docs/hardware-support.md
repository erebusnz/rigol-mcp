# Hardware support

Model capabilities and hardware test results. See the [README](../README.md) for
setup, usage, and the [series support summary](../README.md#supported-hardware).

## Model selection

The server matches the model field from `*IDN?` against
[`models.toml`](../src/rigol_mcp/models.toml). Names are case-insensitive; aliases
are listed explicitly. Unknown models are rejected.

The `idn` tool reports available analog channels. State reads and channel operations
use the connected model's channel count. Identity is cached for the connection and
refreshed after disconnect or communication failure.

## Model catalog

| Models | Analog channels | Manufacturer source |
|---|---:|---|
| DS1054Z | 4 | [DS1000Z datasheet, models and key specifications](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/DS1000Z_DataSheet_EN.pdf) |
| DS1074Z, DS1074Z-S, DS1104Z, DS1104Z-S † | 4 | [Rigol 1000Z series overview](https://www.rigolna.com/products/digital-oscilloscopes/1000z/) |
| MSO1074Z, MSO1074Z-S, MSO1104Z, MSO1104Z-S | 4 | [DS1000Z programming guide, document overview](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DS1000Z_ProgrammingGuide_EN.pdf) |
| DS1074Z Plus, DS1074Z-S Plus, DS1104Z Plus, DS1104Z-S Plus | 4 | [DS1000Z datasheet](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/DS1000Z_DataSheet_EN.pdf) |
| DHO802, DHO812 | 2 | [DHO800 product table](https://www.rigolna.com/products/digital-oscilloscopes/dho800/) |
| DHO804, DHO814, DHO824 | 4 | [DHO800 user guide, document overview](https://www.rigol.com/dam/global/downloads/brochures/zh/user-manual/oscillosopes/DHO800_UserGuide_CN.pdf) |
| DHO914, DHO914S, DHO924, DHO924S | 4 | [DHO900 product table](https://www.rigolna.com/products/digital-oscilloscopes/dho900/) |
| DHO1072, DHO1102, DHO1202 | 2 | [DHO1000/4000 programming guide, document overview](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DHO10004000_ProgrammingGuide_EN.pdf) |
| DHO1202U / DHO1204U | 2 / 4 | [DHO1000U user guide, p.10](https://download.rigol.com/en/Manual/Digital%20Oscilloscope/DHO1000/DHO1000U_UserGuide_EN.pdf) |
| DHO1074, DHO1104, DHO1204, DHO4204, DHO4404, DHO4804 | 4 | [DHO1000/4000 programming guide, document overview](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DHO10004000_ProgrammingGuide_EN.pdf) |
| DS2102A, DS2202A, DS2302A, MSO2102A, MSO2102A-S, MSO2202A, MSO2202A-S, MSO2302A, MSO2302A-S | 2 | [Rigol MSO/DS2000A datasheet, models and ordering information](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MSODS2000A_Datasheet_EN.pdf) |
| DS2072A, DS2072A-S, DS2102A-S, DS2202A-S, DS2302A-S, MSO2072A, MSO2072A-S † | 2 | [Rigol MSO/DS2000A datasheet](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MSODS2000A_Datasheet_EN.pdf) |
| MHO934, MHO954, MHO984 | 4 | [MHO900 product overview and model list](https://www.rigol.com/intl/products/oscilloscope/MHO900.html) |
| MHO98 special edition | 4 | [MHO98 datasheet, p.6](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHO98-DataSheet.pdf) |
| MHO2024, MHO2034 | 4 | [MHO2000 programming guide, p.2](https://download.rigol.com/en/Manual/Digital%20Oscilloscope/MHO2000/MHO2000_Programming%20Guide_EN.pdf) |
| DHO5054, DHO5104 | 4 | [MHO/DHO5000 datasheet, p.9](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHODHO5000_DataSheet.pdf) |
| DHO5058, DHO5108 | 8 | [MHO/DHO5000 datasheet, p.9](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHODHO5000_DataSheet.pdf) |
| MHO5054, MHO5104 | 4 | [MHO/DHO5000 datasheet, p.9](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHODHO5000_DataSheet.pdf) |
| MHO5056, MHO5106 | 6 | [MHO/DHO5000 datasheet, p.9](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MHODHO5000_DataSheet.pdf) |
| DS9202, MSO9402 | 2 | [MSO/DS9000 user guide, p.11](https://www.rigol.com/dam/global/downloads/brochures/en/user-manual/oscillosopes/MSODS9000_UserGuide_EN.pdf) |
| DS9204, DS9404, DS9604 | 4 | [MSO/DS9000 user guide, p.11](https://www.rigol.com/dam/global/downloads/brochures/en/user-manual/oscillosopes/MSODS9000_UserGuide_EN.pdf) |

† Exact-model references are pending; the linked sources describe the series.

The Plus models also accept the hyphenated spellings listed on
[Rigol's DS1000Z product page](https://www.rigol.com/intl/products/oscilloscope/DS1000Z.html).

## Hardware tests

| Model | Connection | Tested functions | Report |
|---|---|---|---|
| DS1054Z | USB, WinUSB/pyvisa-py | Identification, screen waveform, measurement, screenshot | [PR #1](https://github.com/erebusnz/rigol-mcp/pull/1#issuecomment-4654467429) |
| DS1104Z | USB, pyvisa-py | Four-channel state and RAW export, acquisition information, channel validation, reconnect, timeout recovery | [PR #3](https://github.com/erebusnz/rigol-mcp/pull/3), [PR #4](https://github.com/erebusnz/rigol-mcp/pull/4), [PR #8](https://github.com/erebusnz/rigol-mcp/pull/8) |
| DHO924S | LAN, firmware `00.01.05` | Acquisition controls, channel/timebase/trigger configuration, AWG-loopback measurements, cursors, screenshots | `irudoy`, [PR #1](https://github.com/erebusnz/rigol-mcp/pull/1#issuecomment-4650396253) |
| DHO804 | Not specified | Raspberry Pi CM5 deployment with Caddy HTTPS and Claude Code | `4pplet`, [PR #7](https://github.com/erebusnz/rigol-mcp/pull/7) |
| DS2202A | USB, NI-VISA | Identification and reconnect | [PR #5](https://github.com/erebusnz/rigol-mcp/pull/5) |
| DHO814 | Ethernet, firmware `00.01.05` | Measurements, cursors, protocol decoding, recording/replay, RAW transfers in the `gloveboxes/rigol-mcp` fork | Dave Glover, [acceptance report](https://github.com/gloveboxes/rigol-mcp/blob/ffce0bc501ddffcf2f99132c0fb19ebf68bd9dc4/docs/reports/dho814_advanced_mcp_acceptance_report.json), [article](https://techcommunity.microsoft.com/blog/educatordeveloperblog/connecting-github-copilot-to-my-oscilloscope/4555545) |

## Adding a model

Add the exact uppercase name, driver `family`, and positive `analog_channel_count`
to `[models]` in `models.toml`, and add its manufacturer reference to the table above.
Aliases must point to a canonical model name. A new SCPI dialect requires a driver
in [`drivers.py`](../src/rigol_mcp/drivers.py).
