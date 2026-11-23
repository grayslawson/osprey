# 005 Easy setup: one command, one link, one wizard

## Outcome

A new user runs one command, opens the link it prints, follows the wizard and
ends with an adopted camera, a Frigate camera block and a green health check.
No environment variables, no log grepping for a token, no restart, no source
checkout, no hand-edited ONVIF user.

## Requirements

1. **Zero-config start.** No required environment variables. The advertised
   host is detected from the default-route interface and confirmed in the
   wizard. The Compose file defaults to the image of its own release. The
   wizard opens by itself on first run and the log shows a ready-to-open link
   with the one-time token. Saving restarts Osprey onto the new configuration.
2. **Find and adopt the camera.** UniFi discovery (UDP 10001) lists cameras
   with model and firmware, flagged tested or untested. Manual IP entry is the
   fallback. A camera still managed by a controller is flagged with the steps
   to release it. Adoption runs from the wizard (`ubnt`/`ubnt` or a typed
   password), and `osprey adopt` stays available inside the image.
3. **Frigate config done for you.** New installs name the ONVIF user
   `frigate`, so autotracking works without a rename. The wizard shows a
   ready-to-copy camera block and can merge it into Frigate through its config
   API, with a diff preview, a backup and no silent overwrite.
4. **Built-in health check.** The last step shows camera adopted, video
   flowing, ONVIF reachable and Frigate reachable, each failure linked to its
   troubleshooting entry.

## Invariants

- Existing installs behave as before: a configured host, or a host given by
  flag or environment on a state directory already in use, never opens the
  wizard. `OSPREY_SETUP=0` never opens it.
- The setup token is single use (the save consumes it), expires after an hour
  (a new link is logged) and exists only while no host is configured. Home
  Assistant ingress, which has already authenticated its user, needs no token.
- Steps after the save use the normal admin session and CSRF token.
