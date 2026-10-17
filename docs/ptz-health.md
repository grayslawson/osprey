# PTZ health, recovery, and the home preset

This guide explains how Osprey keeps the camera's PTZ channel truthful and
recoverable, what the operator console and `/healthz` report, and how the
`home` preset that Frigate returns to is provisioned and persisted.

## What can go wrong

The camera opens a dedicated `ptz1` websocket back to Osprey. That socket can
stay connected, with keepalives still flowing, while the camera's PTZ process
has stopped answering position queries. A socket that is still open does not
prove the PTZ process is alive. Only a motor-state push or a
`GetCurrentPosition` reply proves that.

Before this change, a move that never reported its final position put ONVIF
`GetStatus` into `UNKNOWN` with `motor state timeout`, and that state cleared
only when the camera next sent a position. When the camera had stopped
answering, the state stayed stuck until Osprey was restarted.

## Motion state rules

- A move to the position the camera already holds finishes immediately.
- If the camera reaches the target but still reports motor activity,
  `MoveStatus` stays `MOVING` for at most 2 s, which is shorter than Frigate's
  10 s autotracking wait. A target frame marked `ignoreActivity` settles the
  move at once.
- Once real motion has been seen, a final motor frame away from the target ends
  the move and logs a warning (`PTZ move settled off target`). Position-query
  replies do not count as that final frame, because they carry no activity
  word.
- A move is held for at most 15 s. When that time runs out, the state becomes
  `UNKNOWN` (`motor state timeout`) and Osprey asks the camera for its
  position. The first fresh reply clears the error.

## Watchdog and recovery

Osprey counts every position request it sends on `ptz1`, whether it came from
the watchdog or from an ONVIF `GetStatus` call made by Frigate. Any motor reply
or push resets the count. Osprey recycles the `ptz1` socket when
`max_unanswered` requests (default 3) have gone unanswered, or when the oldest
unanswered request is more than `reply_timeout_sec` old (default 30 s of
silence). The existing recovery then
sends Disable/Enable PTZ on the management channel. Recovery retries are
bounded and back off exponentially (up to 5 attempts, at most 30 s apart). A
camera that reconnects but still never answers uses up the same attempt
budget, so the loop is never tight. After recovery gives up, it starts again
5 minutes later.

While the state is `UNKNOWN`, or while a request is unanswered, Osprey asks for
the position every `probe_interval_sec` (2 s). A healthy idle channel is asked
every `idle_probe_sec` (60 s) so that a silent failure is found before the next
move needs the camera.

All of these values can be set in `cuckoo.json`:

```json
{
  "ptz": {
    "auto_home": true,
    "reply_timeout_sec": 30.0,
    "max_unanswered": 3,
    "probe_interval_sec": 2.0,
    "idle_probe_sec": 60.0,
    "presets_file": null
  }
}
```

The operator console's **Recover PTZ** button sends `POST /control/ptz/recover`.
Like the other `/control/*` routes, it requires the admin session and the CSRF
token. It recycles the channel immediately and starts a fresh attempt budget,
even if an earlier recovery gave up.

Log lines to alert on: `PTZ watchdog:` (ERROR), `PTZ recovery timed out`
(ERROR), `PTZ motor state timeout` (WARNING), `PTZ move refused:` (WARNING,
rate-limited to once every 30 s per reason). RTSP client disconnects are now
logged at DEBUG.

## Health endpoint

`GET /healthz` on the ONVIF port needs no login and returns JSON. The status is
`200` when Osprey is usable and `503` when no camera is connected or PTZ is
degraded. The response holds state and counters only: no MAC address, IP
address, preset name, or credential.

```json
{"status":"degraded","camera":true,"adopted":true,"uptime_sec":5120,
 "ptz":{"ptz":true,"channel":"up","state":"UNKNOWN","error":"motor state timeout",
        "healthy":false,"last_motor_reply_age_sec":42.0,
        "unanswered_position_requests":2,"motion_timeouts":1,"watchdog_recycles":0,
        "recovery":{"active":false,"attempts":0,"max_attempts":5,
                    "waiting_for_callback":false,"exhausted":false},
        "presets":3,"home_preset":true}}
```

For multiple cameras, use `/cameras/{camera-id}/healthz`. When MQTT is enabled,
the same state is also published to `osprey/<device>/state/ptz` (see
[MQTT](mqtt.md)).

## Home preset and persistence

The camera stores preset slots itself, but it has no known command for listing
them back. Osprey now saves its own table of named positions in
`osprey-presets.json`, next to the TLS certificate in the writable state
directory. You can change the location with `ptz.presets_file` or the
`OSPREY_PRESETS_FILE` environment variable. Osprey reloads the table when the
camera connects, then writes the slots back to the camera once per process
start. Writing a slot never moves the camera.

If no preset named `home` exists (case-insensitive) when the first confirmed
position arrives, Osprey saves the current position as `home` and logs a
warning. This does not move the camera. To turn it off, set
`ptz.auto_home: false`. To replace the automatic home, point the camera where
you want it and click **Save current view as home**, or call ONVIF
`SetHomePosition`. ONVIF `GotoHomePosition` moves to that preset, and
`HomeSupported` is now advertised.

After `home` is first created, restart Frigate or reinitialize its ONVIF
connection so that it reloads the preset list. See
[Frigate integration](frigate-integration.md#named-position-discovery-and-refresh).

### Upgrade and rollback

Upgrading needs no action. The first start creates `osprey-presets.json`. Any
presets saved before the upgrade only ever lived in memory, so save them again.
To roll back, revert the release. The older release ignores the extra file and
the `ptz` config block, but it rejects nothing, because the older config loader
does not validate unknown keys.
