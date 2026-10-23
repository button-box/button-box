# Who can turn on the Button Box microphone?

Audit date: **12 October 2026**.
Audited source: **`e1c817ee334def2628a62bce44ca50a050e93f3d`**, plus the uncommitted microphone safeguards described here. This is an audit of source code, not certification of a running box. The branch descends from `box4-release-20261006` (`196de69254ac2f4c8cdc39179dcb70bbe700e76a`) and contains later changes. Neither revision was verified on a device during this audit. The safeguards have not been released or installed by this work.

## For an owner

**In the reviewed Button Box application, only a physical button interaction can start recording.** Receiving a message, opening the setup page, changing a setting, tapping a family card, or receiving a Cloud command cannot start a recording by itself. This applies to both Cloud boxes and self-hosted boxes using wacli.

With **Hold to talk**, holding the button past its gesture check starts recording; releasing it stops recording. With **Tap to talk**, a press starts a session. Cloud Button Box capture starts alongside the sound; other voices and wacli wait for it. If that session plays an incoming message and **Invite reply** is selected, recording can begin after playback without a second press. That is part of the session the first press started. With **Play only**, playback ends without recording. Review-before-send plays your recording back and requires another press to send it; review does not make a new recording. Recording is limited to the configured maximum of 30, 60, or 120 seconds.

Cloud Button Box now turns on the lamp and starts one short cue alongside microphone capture, after the same debounced physical press. Its tap flow does not wait for the start press to be released. The start press must be released before a later tap can stop recording. Other voices and wacli still finish the start sound before opening the microphone. An immediate start-player failure prevents capture; a later playback failure aborts capture and deletes the recording. Cue completion is required before a recording can leave capture. The light also has other meanings, so an illuminated light alone does not prove the microphone is on. Neither the light nor the sound is a hardware microphone disconnect.

**There is an important limit:** someone with administrator access to the box can run different software or replace this software, including software that records without a button press. The developer's ordinary SSH access, where it exists, is administrator access. An attacker who steals that access or takes over a process allowed to use the audio device could also bypass the application's rules. This audit cannot honestly promise protection against those cases. A physical microphone disconnect is separate future work.

### What the box accepts from the internet

A Cloud box makes authenticated connections to Button Box Cloud to receive messages, settings, family-card setup requests, playback previews, reactions, and quiet/mute instructions. These are a fixed set of operations. There is no Cloud operation to record, run a shell command or upload executable code. The updater follow-up adds exact-version install requests, which require a local physical hold and independent signature verification. A notification connection only tells the box to check for work. A self-hosted box uses its linked WhatsApp account through wacli instead of contacting Button Box Cloud.

Settings can change how the *next button session* behaves, including whether it invites a reply after playback. They cannot create the button permission needed to open the microphone. The permission is held only inside the current process and thread, works once, and is discarded when the session ends. It is not stored in a file or accepted from the network.

Voice-message handling through the Cloud is outside this audit. Downloaded messages and spoken prompts are playback inputs, not microphone inputs.

### Updates and remote support today

Today an operator can install software through SSH/provisioning or run the existing local bounded installer as an administrator. That installer checks its file manifest and has rollback behavior; it is not an owner-approved, signed, public-release updater. Existing administrator access can replace the audited code. A public release tag or hash identifies source; it does not, by itself, enforce which software a box runs.

The original microphone audit did not implement signed updates. The updater follow-up below adds that source implementation and pins the public release and emergency keys; deployment and real-device recovery tests remain required. Removing remote support also needs its own rollout and verification. Tailscale, when configured, carries ordinary SSH and a private HTTPS dashboard; disabling Tailscale SSH does not disable ordinary SSH through it. Removing Tailscale does not remove administrator keys or LAN SSH access.

### Local network access

The normal local page listens on the box's Wi-Fi address on TCP port 80. It has **no login**. Its address and browser-origin checks help reject requests from unrelated websites, but are not authentication. Someone on the same network can deliberately use its allowed address to read locally available information and operate the page's controls. Cloud mode blocks the old self-hosted account/contact management controls. Neither mode offers a recording control.

During setup, the portal listens on port 80 on all IPv4 interfaces. The hotspot portal has firewall restrictions; the home-network portal is reachable on connected networks, with an allowed-host check and no login. Treat the setup hotspot password and your home Wi-Fi network as access boundaries. Do not port-forward these pages onto the internet.

This checkout contains no analytics server or service for port **8082**. A live-device check is still needed to rule out an old installed service or an unrelated listener.

### How you can check

1. Compare your installed version and commit, shown on the local page when available, with the public source and release notes. Ask the operator to verify installed file hashes too: a displayed version is not proof that the files are unchanged.
2. Keep the button untouched while receiving a message, changing settings, previewing a sound, and tapping a family card. None should start microphone capture. Then test a deliberate recording and check the start sound, light, and stop behavior. Test the selected playback/reply mode as well.
3. Read the [capture guard](../messagebox/mic_capture.py), [button flow](../messagebox/button_send.py), and [regression tests](../tests/test_mic_access.py), or ask someone you trust to review them. The commands and detailed inventory below make that review repeatable.
4. Ask for an inventory of SSH users, authorized keys, administrator permissions, listening ports, and remote-support access on your actual box. Keep those results private. Source code cannot tell you which keys are currently installed.

## Technical appendix

Line references below refer to this audited worktree, including the safeguards. Tests are excluded from the capture inventory. Search covered all `messagebox/`, `scripts/`, `systemd/`, and `config/` sources, including Python, shell, unit files, and embedded Python. External OS tools and dependencies were inspected as invocation boundaries, not independently audited implementations.

### Audio-input inventory

| Source | Trigger and route | Physical button required? |
| --- | --- | --- |
| `messagebox/mic_capture.py:52` (`open_microphone`) | The only invocation of `arecord`: raw mono signed 16-bit PCM, 16 kHz, operator-configured `MIC_DEV` supplied to `-D`. No shell. | Yes: active, unused, thread-bound permit from `capture_session`, which checks a GPIOzero `Button` across a 60 ms debounce interval. |
| `messagebox/button_send.py` (`capture_guided_recording`, `PiGuidedIO.record`) | Consumes the permit alongside the Cloud Button Box start cue, or after synchronous `rec_go` in other modes. Captures within a standalone or reply session. | Yes, including the explicitly selected reply-after-playback flow. Calling either recording function without a permit fails before cue playback or subprocess creation. |
| `messagebox/button_send.py:2207`, `:2219`, `:1957`, `:2025`, `:2355` | Debounced main-loop GPIO press -> permission scope -> Hold or Tap flow -> `PiGuidedIO`. The sender/background threads only send already recorded audio. | Yes. Initial held-at-boot input is discarded until release. The permission expires on return or failure and cannot be reused. |
| `messagebox/button_send.py:525`, `:571` | A physical press interrupts a spoken family-card prompt, then enters the same guarded handler. A card presentation alone only selects a recipient or plays feedback. | Yes. The new permission check also debounces this interruption. |
| `messagebox/guided_reply.py:559` | Pure session orchestration invokes `io.record`, optionally after incoming playback in an already button-started session. Review and approval only use playback/button methods. | Yes when bound to the real `PiGuidedIO`; this pure module cannot open any hardware itself. |
| `scripts/dev/hardware-test.sh:133`, `:155`, `:158` | Operator-only interactive microphone test: release the button, then press within 15 s; same permission guard and capture entry; four seconds, local playback, no message send. | Yes after this fix. Previously terminal Enter alone started its separate `arecord` invocation. |
| `scripts/dev/simulate-inputs.py:517`, `:550`; `scripts/dev/simulate-selected-message.py:574`, `:643` | Developer simulators substitute scheduled synthetic inputs and call the button flow. They are not installed by setup/provisioning. | Synthetic inputs cannot obtain a permit on a real Pi: they are not GPIOzero `Button` instances. Previous real-microphone simulation runs now stop at this guard. Hardware-free tests may inject modeled GPIO, as usual. |

No other capture-tool invocation, Python audio-capture library, direct capture PCM-device open, `ffmpeg -f alsa`, or ALSA capture backend was found. `aplay -D` opens output, not input; ffmpeg/ffprobe operate on downloaded or recorded files. `amixer ... cap` in the button units enables a mixer control, but does not open a capture stream. `scripts/install/audio_config.py` detects cards through `/proc/asound` and sysfs without recording.

### Cloud/network and cross-process inputs

Cloud entry: `messagebox/voicepoll.py:348` selects the transport. Cloud polling is `cloud_runtime.py:1327`; authenticated HTTPS requests are `cloud_device.py:175`. Media downloads are restricted to the configured HTTPS API origin and device-media paths (`:311`); redirects are disabled by `device_http.py:9`. WebSocket work hints (`cloud_events.py:14`, `:63`) only wake HTTP polling. Device registration, claims, and heartbeat responses also update account/entitlement/contact/settings state; they are data, not executable instructions.

| Accepted operation/input | Handler | Effect and capture boundary |
| --- | --- | --- |
| `audio` | `cloud_runtime.py:640` | Validates authorized sender, expiry, content type and hash; queues downloaded/transcoded audio. Does not open input. |
| `reaction`, `listened`, `card_prompt`; fetched voice lines | `cloud_runtime.py:865`, `:875`, `:920`, `:989`, `:1072`; `cloud_voice.py:33` | Downloads validated WAVs and records playback requests/receipts. Reactions can play while idle; no recording call. |
| `settings` | `cloud_runtime.py:1152`; `settings.py:264` | Known keys validated; unknown top-level keys ignored and reported. Changes volume, voice, ringtone, recording-session choices, quiet schedules and feedback. Cannot set microphone device, GPIO pin, executable, shell command, or permission token. |
| `nfc_enroll`, `nfc_cancel`, `nfc_unpair` | `cloud_runtime.py:744`, `:1195` | Stages/cancels card enrollment or removes the specified mapping. Enrollment still uses a card presentation; no capture. |
| `preview_ringtone`, `voice_preview` | `cloud_runtime.py:1218`, `:1229`; `audio_requests.py:85` | Bounded playback requests owned by the button process. Allowed request kinds are success, preview, voice preview and settings saved; none is capture. |
| `queue_hold`, `delete_message` | `cloud_runtime.py:1149`, `:1197` | Holds delivery or deletes identified queued/retained media; no capture. |
| `quiet_now`, `mute` | `cloud_runtime.py:1140`; `cloud_quiet.py` | Stops/suppresses output and updates silence state. Mute blocks new button recording sessions; cannot start one. |
| Pair/claim state | `cloud_claim.py`; `button_send.py:2168`, `:2188`, `:2263` | Claim-only Cloud setup loop confirms ownership with the button and never reaches normal capture. In wacli setup voice mode the normal guarded button flow may run. |
| Software updates | `cloud_runtime.py`; `cloud_update.py` | Exact-version `install_update` and bounded `set_update_mode` requests use independent verification and local physical confirmation. No capture permit is created. |
| Shell, arbitrary command, recording | `cloud_runtime.py` | **Not supported.** Unknown command kinds are rejected. |
| wacli/WhatsApp sync and poll results | `syncloop.sh:12`, `:25`, `:54`; `voicepoll.py:182`, `:203`, `:327` | WhatsApp network connection runs in wacli; read-only media download uses a generated local path; allowlisted chats feed the queue. Optional webhook is an outbound receipt POST, not a shell callback. |
| Physical NFC reader and runtime sidecars | `nfc.py:212`; `nfc_state.py`; `button_send.py:525` | Card UID selects/enrolls/removes a mapping or triggers a reaction. Atomic private enrollment/selection/announcement files coordinate the processes. A card or a written sidecar cannot mint microphone permission. |
| Shared settings, silence, audio requests, queues/outbox, receipts, pairing pause file | `runtime_paths.py`; `audio_requests.py:85`; `syncloop.sh:7`; `button_send.py` | Fixed local files, locks and account checks carry bounded state between processes. No file is interpreted as a recording token, executable, or shell command. Writers sharing a Linux user remain a trust boundary, discussed below. |

### Local HTTP and Unix endpoints

Normal HTTP implementation: `dashboard/app.py:1200`, `:1319`, `:1353`, `:1712`, `:1721`. Default bind is the current IPv4 address of `wlan0`, TCP 80 (`config/env.example:24`). An optional exact device-local Tailscale Serve hostname adds a loopback listener and private HTTPS 443 proxy. Operator configuration can override the bind; setup resets it to `wlan0:80`.

All browser routes require the recognized Host. Mutations reject cross-site browser requests and mismatched supplied Origin, with bounded form/JSON parsing. No password, login, bearer token or session cookie protects these browser controls. An absent Origin is allowed for deliberate non-browser clients. These restrictions are **not LAN authentication**. The receipt webhook uses HMAC authentication separately.

| Normal local routes | Purpose/auth distinction |
| --- | --- |
| `GET /`, `/cloud-connect`, fixed static assets; `/api/state`, `/api/settings`, `/api/data`, `/api/wifi-change` | UI, status/settings/activity and connectivity. No login. |
| `GET /api/whatsapp`, `/api/recipients`, `/api/nfc-runtime`, `/api/contacts`; `POST /whatsapp/pair/start`, `/whatsapp/pair/cancel`, `/whatsapp/unlink` | Self-hosted status/account setup. Cloud mode rejects these legacy management routes. |
| `POST /recipients/refresh`, `/recipients/defer`, `/recipients/select`, `/recipients/add`, `/recipients/select-number`, `/recipients/add-number`, `/recipients/remove`, `/recipients/default`, `/recipients/rename`; `/nfc/enroll`, `/nfc/cancel-runtime`, `/nfc/unpair-presented`; `/api/contacts`, `/api/listeners` | Self-hosted people/listener/card management; Cloud mode rejects the legacy management family. |
| `PUT /api/settings`; `POST /api/ringtone-preview`, `/api/wifi-change`, `/api/ring` | Save settings, speaker preview, request bounded Wi-Fi change, queue an alert. No capture control. |
| `GET /audio/<opaque token>` (with optional `played`, `hold`, or `trash` query flag); `POST /api/requeue`, `/api/hold`, `/api/resume`, `/api/delete`, `/api/reinstate` | Play/download or move existing authorized audio; opaque local filename mappings, not arbitrary file paths. |
| `POST /api/wacli-receipt` (`dashboard/app.py:1510`) | Body size capped; configured HMAC-SHA256 signature required. Disabled without a secret. Only ingests played receipts; no capture. |

Setup is the WSGI application at `onboarding/app.py:839`. Both portal units bind **`0.0.0.0:80`**. The hotspot portal is filtered by `config/onboarding/firewall.nft:1`: Wi-Fi DHCP, hotspot DNS, hotspot-subnet HTTP to `10.41.0.1`, mDNS and limited ICMP; remaining setup-subnet traffic is dropped, other port-80 ingress is dropped except loopback. This temporary firewall is not a general runtime firewall. The home portal has no equivalent interface restriction. Both accept only configured hostnames (plus the hotspot address in hotspot mode); optional tailnet origin requires a loopback HTTPS proxy.

Setup routes have no login. State/mode gates, Host checks, form parsing and cross-site checks constrain actions; `/wifi/connect` relies on hotspot/mode access rather than an Origin check. See `onboarding/app.py:341`, `:863`, `:1043`.

| Setup routes | Purpose |
| --- | --- |
| `GET /`, `/cloud-connect`, static assets and captive-portal probe routes | Pages/handoff and OS connectivity probes. |
| `GET /api/state`, `/api/data`, `/api/settings`, `/api/networks`, `/api/recipients`, `/api/nfc`, `/api/cloud-claim`, `/api/cloud-claim/qr` | Bounded setup status, network scan, self-hosted people/cards or Cloud claim/QR status. Cloud rejects self-hosted data/management routes. |
| `PUT /api/settings`; `POST /api/ringtone-preview`, `/wifi/connect`, `/wifi/change`, `/api/cloud-claim/start`, `/api/cloud-claim/cancel` | Settings/playback, Wi-Fi and claim setup. Claims still require physical confirmation. |
| `POST /whatsapp/pair/start`, `/whatsapp/pair/cancel`, `/whatsapp/unlink`; `/recipients/refresh`, `/recipients/defer`, `/recipients/select`, `/recipients/add`, `/recipients/select-number`, `/recipients/add-number`, `/recipients/remove`, `/recipients/default`, `/recipients/rename` | Home-network self-hosted account and recipient setup, gated by setup phase; blocked in Cloud mode. |
| `POST /nfc/start`, `/nfc/assign`, `/nfc/retry`, `/nfc/reassign`, `/nfc/next`, `/nfc/cancel`, `/onboarding/complete` | Card setup and completion request. Bounded local workers/root completion service; no capture operation. |

Other process interfaces:

- Pairing worker: `/run/messagebox-whatsapp-pairing/worker.sock`, AF_UNIX, mode `0660`, group `messagebox-onboarding` (`onboarding/whatsapp.py:1181`, `:1270`). Exact actions: status/start/cancel/unlink/relink, activity/recipient state/list/defer/select/add/remove/default/rename, including phone-number variants. Only fixed wacli operations; no capture or shell command action.
- NFC setup worker: `/run/messagebox-onboarding-nfc/worker.sock`, AF_UNIX, mode `0660`, same group (`onboarding/nfc.py:414`, `:455`). Exact actions: status/start/retry/reassign/next/cancel/finish and assign with a recipient token. No capture action.
- Comitup/NetworkManager: system D-Bus, not a product TCP API. `config/onboarding/comitup-dbus.conf` limits the onboarding user to access-points/state/connect/delete-connection; other non-root users are denied. Names/passwords remain data arguments to the D-Bus call.
- No repository systemd `.socket` activation unit or product cron entry exists. The two Unix sockets are created by their worker processes.

### Services, timers and privileged transitions

| Units (under `systemd/`) | User and inputs |
| --- | --- |
| `messagebox-button.service:9`; `onboarding/messagebox-onboarding-button.service:11` | `messagebox`; GPIO/audio/settings groups; Python button process. Setup sets `MSGBOX_CLAIM_ONLY=1`, used by Cloud mode. Startup `amixer` operates mixer controls, not capture. |
| `messagebox-poller.service:9`, `messagebox-sync.service:8`, `messagebox-nfc.service:10`, `messagebox-dash.service:9` | `messagebox`; poller/network, wacli sync, physical NFC, LAN HTTP respectively. They inherit the service user's installed supplementary groups, including audio. NFC adds I2C/GPIO; dashboard has bind-port capability and `NoNewPrivileges`. |
| `onboarding/messagebox-onboarding-home.service:13`; `onboarding/comitup-web.service.d/messagebox.conf` | `messagebox-onboarding`; HTTP portal, settings/audio groups, `PrivateDevices`, fixed writable paths and bind-port capability. No recording code. |
| `onboarding/messagebox-whatsapp-pairing.service:10`; `onboarding/messagebox-onboarding-nfc.service:11` | `messagebox`; group `messagebox-onboarding`; restricted Unix sockets; pairing has `PrivateDevices`, NFC needs GPIO/I2C/audio. |
| `messagebox-audio-detect.service:6`, `messagebox-mode-reconcile.service:7`, `messagebox-wifi-watchdog.service:7` | Default root (no `User=`). Oneshot device detection, fixed mode/service reconciliation, fixed NetworkManager recovery. No HTTP shell handler or capture. |
| `messagebox-wifi-change.service:8`; `onboarding/messagebox-onboarding-complete.service:8`, `messagebox-onboarding-voice-gate.service:8`, `messagebox-wifi-reset.service:9` | Root; fixed transactional Wi-Fi change, setup completion, voice-mode activation, or physical boot-reset check. Fixed service names and paths. File requests can initiate these bounded operations, not arbitrary commands. |
| `messagebox-wifi-change.path:5`; `messagebox-mode-reconcile.path:5`; `onboarding/messagebox-onboarding-complete.path:5`; `messagebox-onboarding-voice.path:5` | Watch Wi-Fi request, root mode marker/pending request, completion request, or voice/Cloud state respectively. Their named services validate the relevant requests. |
| `messagebox-wifi-watchdog.timer:7` | Every 30 seconds during runtime; NetworkManager recovery only. No recording timer. |
| `messagebox.target`, `onboarding/messagebox-onboarding-voice.target`; `messagebox-mode-generator` | Group/start predefined services according to the root-owned boot-mode marker. Starting the button service alone does not initiate capture; startup waits for release and a press. |

### Shell/subprocess and download review

Every runtime subprocess/exec site and operator/install shell script was reviewed. Runtime calls use argument lists and fixed executables, with no `shell=True`, `os.system`, network-provided Python `eval`/`exec`, or command-string execution. The developer settings-contract tool's AST `exec` executes locally selected source definitions, not a Cloud response.

| Calls reviewed | Network-influenced values and result |
| --- | --- |
| `button_send.py:172`, `:179`, `:190`, `:715`, `:747`, `:878`, `:923`, `:1387`, `:1722`; `cloud_quiet.py:91`; `mic_capture.py:52` | Playback/transcode, mixer volume, wacli voice/reaction/presence and capture. Recipient/message fields are separate arguments; routing is checked against saved account/contacts. Audio paths are generated local paths. Executable, capture device, speaker device and EQ filter are operator-only configuration, not Cloud settings. |
| `voicepoll.py:108`, `:136`, `:182`, `:203`, `:231` | Fixed wacli download/search, ffprobe and ffmpeg; remote chat/message IDs are separate arguments. Download destination is generated locally and the returned server/client path is not adopted. Queue filenames must be basenames ending in `.wav`; Cloud filenames use locally generated hashes. No shell interpolation/path traversal found. |
| `cloud_voice.py:33`; `cloud_runtime.py:920`, `:927`, `:941`; Cloud ACK/intent paths | Voice-line IDs, audio hashes and text hashes are constrained hex strings; other identifiers are validated or hashed before forming paths. Cached WAVs have size/hash/format checks and reject symlinks. No arbitrary remote filename or traversal accepted. |
| `dashboard/app.py:199`, `:257`, `:277`, `:329`, `:350`, `:365`; `onboarding/app.py:827`; `onboarding/nfc.py:132` | Fixed status queries, chat refresh/discovery, mixer and speaker playback; ringtone/voice names are allowlisted. HTTP media names resolve through local opaque-token mappings. No supplied command or input device. |
| `onboarding/whatsapp.py:734`, `:899`; `contacts.py:719`, `:741` | Pairing uses normalized phone data and fixed arguments; Unix actions select predefined operations. Contact CLI execs the fixed service-user Python command with operator arguments. No shell callback. |
| `wifi_change.py:102`, `:123`; `wifi_watchdog.py:39`; `onboarding/connectivity.py:64` | Fixed nmcli/ip/iw/getent/curl operations. Wi-Fi name is an argument; password goes via stdin, not command interpolation. NetworkManager UUIDs are constrained before reuse. These are privileged network-management operations, not arbitrary execution. |
| `onboarding/initialize.py`, `reset.py:83`, `completion.py:89`, `mode.py:176`, `voice_gate.py:32`; mode generator | Fixed mkdir/install/service/NetworkManager operations and root-controlled mode state. No network-selected executable or systemd unit name. |
| `syncloop.sh:12`, `:25`, `:54` and unit shell checks | Bash array for fixed wacli command. Optional webhook URL/secret come from operator config; no eval. Unit `/bin/sh -c` strings are fixed mode checks; environment values are quoted. |
| `scripts/provision.sh`, `provision-tailscale.sh`, `dev/reprovision.sh`, `messageboxctl`, `scripts/commands/*`, `scripts/install/*`, developer helpers | Administrator-run provisioning/recovery/download tools intentionally can change code/system services. Targets/hostnames and install paths are validated; downloaded installers/packages remain supply-chain trust inputs. `bounded_update.py:1090` accepts a local manifest with allowlisted installed paths/hashes and fixed service control. These tools are not reachable through a Cloud command or product HTTP command-execution endpoint. |

This is a source review, not a proof that media decoders, wacli, GPIOzero, the operating system or dependencies contain no exploitable bugs. Parsing untrusted media and network responses remains an attack surface. A compromised dependency/process with audio access could bypass the guard; the shared-user finding below remains open.

### Local users, passwords, sudo and expected listeners

`README.md:199` directs Imager to create a non-root administrator and enable SSH, preferably with a dedicated public key. `scripts/provision.sh:14` accepts an existing `user@host`; it does not create that administrator, install SSH keys, set a password, disable password login, or change sshd configuration. `docs/remote-support.md:17` requires the operator's key and passwordless sudo for remote support; `scripts/provision-tailscale.sh:71` checks `sudo -n true`.

`setup.sh:184` and `:201` create/validate `messagebox` and `messagebox-onboarding` system users with `/usr/sbin/nologin`. No shared/default login password or authorized key is set in these scripts. Setup adds audio/GPIO/I2C/settings groups (`:228`, `:229`). **No sudoers file or NOPASSWD rule is installed by this repository.** The actual administrator's NOPASSWD scope, password status and authorized keys depend on the imaged OS and later operator changes; they remain unverified. Service-account nologin is not isolation between processes running as that account.

Expected listeners depend on OS and enabled mode:

| Listener | Expected scope/source |
| --- | --- |
| TCP 22 | Ordinary OpenSSH, if enabled in Imager; actual IPv4/IPv6 bind and authentication must be checked. Optional Tailscale makes this reachable to permitted tailnet peers too. |
| TCP 80 | Runtime: Wi-Fi IPv4 only, optionally loopback. Setup: all IPv4 interfaces, with hotspot firewall restrictions as above. |
| TCP 443 | Optional Tailscale Serve HTTPS on the tailnet, not a public Funnel service. Verify actual mapping and ACLs. |
| UDP 5353 | Avahi/mDNS device discovery; actual interface scope comes from OS configuration. |
| UDP/TCP 53 and UDP 67 | Hotspot DNS/DHCP while Comitup/NetworkManager hotspot is active. Not product HTTP or capture APIs. |
| Tailscale UDP transport | Conditional on Tailscale installation; port and bind depend on the daemon/OS. Check `ss -lunp`, not just TCP. |
| AF_UNIX worker sockets | Pairing and NFC setup workers at the group-restricted `/run/` paths listed above; system D-Bus is OS-managed. |
| TCP 8082 | No implementation, unit, setup input or listener configuration in the audited product source. A running listener would be unexpected and requires investigation. |

No product cron jobs were found. OS/package cron jobs, timers, units, firewall rules and extra accounts can only be inventoried on the device.

### Findings and disposition

Severity describes the privacy impact if the stated prerequisite holds; it is not a claim of a remotely exploitable vulnerability.

| Severity | Location | Finding | Fixed / reason and remaining risk |
| --- | --- | --- | --- |
| Medium | `button_send.py:1779`; `mic_capture.py:29`, `:45`, `:52` | Capture entry relied on callers having followed the physical-button flow. | **Fixed here:** real-button session, one-use permission, same thread, scope expiry; no capture before permission/cue. Defense against unintended code paths, not hostile code in the process. |
| Medium | `scripts/dev/hardware-test.sh:133` | Developer helper recorded after terminal Enter, including over SSH. | **Fixed here:** physical press required; shared capture entry; bounded local test and playback. |
| Medium | `scripts/dev/simulate-inputs.py:550`; `simulate-selected-message.py:643` | Synthetic button schedules could reach real microphone capture during operator experiments. | **Fixed at shared guard:** synthetic input object cannot mint a real-button permit on a Pi. Such microphone simulation runs now stop; use a physical test instead. |
| Medium | `button_send.py:1800`, `:1857`, `:1873` | Notification/hardware failure just after opening capture could bypass cleanup; graceful termination could time out. | **Fixed here:** all post-open work lies inside cleanup; failed notification cannot leave capture running; terminate timeout escalates to kill; LED/state cleanup runs even if pause notification fails. |
| High, conditional | `setup.sh:228`, `:229`; runtime units' `User=messagebox`; `docs/remote-support.md:17` | Administrator/root or compromised audio-capable service can independently open ALSA capture or replace software. Several services share the audio-capable user. | **Open:** OS permission/process separation, remote-access changes and a hardware mic gate require their own design/rollout. Restricting audio without testing speaker, NFC and setup workers could break both transports. No claim of attacker-proof mic exclusion. |
| Medium | `dashboard/app.py:1135`; `onboarding/app.py:839`; portal units | Local browser controls have no login; setup home page binds all IPv4 interfaces. | **Open:** existing local-access model; adding authentication is a product/access change beyond a safe mic fix. LAN users can change settings/manage available audio and self-hosted account data. Host/Origin checks do not authenticate them. No capture endpoint. |
| Medium, conditional | `scripts/provision.sh:14`; `README.md:199` | Admin passwords, keys, sshd policy and sudo scope are outside provisioning's control. | **Unverified:** coordinator must inspect the actual box. The repository supplies no default password/NOPASSWD policy to fix. |
| Informational | `cloud_runtime.py:1246`, `:1355`; subprocess/download inventory above | Cloud contract has no arbitrary-command, shell or microphone operation; update requests use the separate signed and physical-confirmation path; no runtime shell injection or downloaded-ID traversal identified. | **Confirmed in source:** unknown commands rejected. Decoder/dependency vulnerabilities and hostile local writers are not ruled out. |
| Informational | `messagebox/`, `scripts/`, `systemd/`, `config/` search | No port 8082 endpoint remains in this checkout. | **Source confirmed; live unverified:** inspect listeners and stale installed units. |

### Automated validation and wacli impact

`tests/test_mic_access.py` rejects absent/invalid permissions, short/synthetic presses, cross-thread capture, permission reuse and capture after a failed/finished session. It exercises post-open failure cleanup including termination timeout. Its source inventory rejects new capture tools/backends outside the single entry, and its call-graph check rejects new runtime callers of session/recording functions. This is a regression guard, not a complete static proof against dynamically generated code or malicious replacement software.

The normal capture/review contract tests model a physical press and still cover Hold/Tap, review on/off, maximum duration, short/silent deletion, playback, card routing, quiet behavior and listened notices. The shared changes are `button_send.py`, new `mic_capture.py`, setup/provision allowlists and the developer hardware test. **wacli impact:** healthy physical-button sending, playback/reply, family-card routing, quiet hours and listened notices retain their existing behavior; recording additionally requires the session permission, NFC prompt interruption has a 60 ms debounce, and capture failure cleanup is stronger. Terminal/synthetic microphone starts intentionally stop working. No Cloud concept was added to wacli routing. The original mic audit changed no command kind. The signed updater follow-up adds two bounded commands and requires a regenerated fleet contract.

Validation on 12 October 2026: **both commands passed** syntax, Python/shell/frontend lint, **908 Python tests**, and **54 frontend tests**. `git diff --check` also passed. These suites model hardware; they do not verify a deployed device.

```sh
MSGBOX_TEST_TRANSPORT=cloud make check
MSGBOX_TEST_TRANSPORT=wacli make check
```

Physical Pi, GPIO, audio, NFC, Wi-Fi and WhatsApp behavior remain unverified. In particular, run the physical wacli acceptance matrix before any merge to main: receive/play a voice note, send/reply from the box, tap a family card, quiet hours and listened notices.

### Files changed

- Runtime: `messagebox/mic_capture.py`, `messagebox/button_send.py`.
- Installation/operator helper: `scripts/dev/hardware-test.sh`, `scripts/setup.sh`, `scripts/provision.sh`.
- Documentation: `docs/mic-access-audit.md`, `docs/developer-onboarding.md`, `docs/testing.md`.
- Regression tests/fixtures: `tests/test_mic_access.py`, `tests/mic_helpers.py`, `tests/test_recording_modes.py`, `tests/test_card_voice.py`, `tests/test_sound_pack.py`, `tests/test_cloud_quiet.py`, `tests/test_input_simulator.py`, `tests/test_selected_message_simulator.py`, `tests/test_release_manifest.py`.

### Coordinator checks on box 4

Run the following **inside an already authenticated session on the verified box 4**, not against an inferred address. These are read-only inspection commands; keep raw device output private. No live checks or access changes were made during this audit.

```sh
hostname
cat /etc/os-release
sudo ss -ltnp
sudo ss -lunp
sudo ss -lxnp
sudo ss -ltnp 'sport = :8082'
sudo systemctl list-units --all --type=service,socket,timer
sudo systemctl list-unit-files --type=service,socket,timer
sudo systemctl cat messagebox-button.service messagebox-dash.service messagebox-poller.service messagebox-sync.service
sudo systemctl cat messagebox-onboarding-home.service comitup-web.service
sudo systemctl status messagebox.target messagebox-button.service messagebox-poller.service messagebox-nfc.service --no-pager
getent passwd
id admin
id messagebox
id messagebox-onboarding
sudo passwd -S admin
sudo passwd -S messagebox
sudo passwd -S messagebox-onboarding
sudo -l
sudo -l -U admin
sudo -l -U messagebox
sudo -l -U messagebox-onboarding
sudo sshd -T | grep -E '^(port|listenaddress|passwordauthentication|pubkeyauthentication|permitrootlogin|authorizedkeysfile|authorizedkeyscommand|allowusers|allowgroups|authenticationmethods) '
sudo find /root /home /var/lib/messagebox /var/lib/messagebox-onboarding -name authorized_keys -type f -print
sudo ssh-keygen -lf /home/admin/.ssh/authorized_keys
sudo nft list ruleset
sudo systemctl list-timers --all
sudo crontab -l
sudo crontab -l -u admin
sudo crontab -l -u messagebox
sudo crontab -l -u messagebox-onboarding
sudo ls -la /etc/cron.d /etc/cron.daily /etc/cron.hourly /var/spool/cron/crontabs
sudo ls -l /dev/snd
sudo fuser -v /dev/snd/pcm*c
sudo cat /opt/messagebox/release.json
```

If the administrator has a different name/home, use that live account for the `admin`/authorized-key commands and enumerate every login-capable user. Inspect sshd `Match` policies using `sudo sshd -T -C user=admin,host=localhost,addr=127.0.0.1` and the real remote client's address. Enumerate authorized keys for each login account without posting key bytes/comments publicly. An absent `release.json` is unknown provenance, not proof of the expected version; compare installed program hashes against a locally generated manifest from the exact reviewed source.

If Tailscale is installed:

```sh
sudo tailscale status --self=true --peers=false
sudo tailscale serve status
sudo tailscale funnel status
```

For the microphone check, observe `sudo fuser -v /dev/snd/pcm*c` and the matching `/proc/asound/card*/pcm*c/sub*/status` files while idle, during network commands/previews/card presentation, during capture, and after release/timeout/failure/restart. Confirm no active capture stream during the idle/network-only cases and no lingering `arecord` child after recording ends. Also check for hidden extra programs; an `arecord` process search alone cannot detect all capture backends. Verify cues and the physical wacli matrix on the device before claiming owner acceptance.

### Signed updater follow-up

The Cloud software updater adds bounded `install_update {version}` and
`set_update_mode {mode}` commands. It intercepts update confirmation before
`_capture_confirmed_press` opens B71's physical capture session. Confirmation
never creates a capture permit, and every confirmation press is consumed until
release. Tests verify short, held, expired and successful confirmation gestures
never start a microphone process. The only update-health capture-tool invocation
is `runuser -u messagebox -- arecord -l`, which enumerates ALSA devices without
recording. The source inventory locks that command shape separately from the
one permitted microphone entry. See [on-box updates](on-box-updates.md).
