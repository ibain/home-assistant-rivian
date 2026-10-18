# Home Assistant integration for Rivian (Unofficial)

[![GitHub Release][releases-shield]][releases]
![GitHub all releases][download-all]
![GitHub release (latest by SemVer)][download-latest]
[![GitHub Activity][commits-shield]][commits]

An unofficial Rivian integration for Home Assistant, installed through [HACS](https://hacs.xyz/docs/setup/download).

To use this extension, you'll need the following information

- Rivian Username
- Rivian Password
- Rivian Account's MFA/OTP Device (optional, depending on your Rivian account settings)

Your vehicle must be in delivered status and possession for this integration to function.

# Disclaimer

This [Home Assistant](https://www.home-assistant.io/) integration is not affiliated, associated, nor sponsored by Rivian Automotive, Inc.

Any use of this integration is at the sole discretion and risk of the Rivian vehicle owner integrating it into their Home Assistant installation. This owner takes full responsibility for protecting their local Home Assistant installation.

This integration is provided out of love and passion towards the Rivian ownership community. It focuses on building insights and awareness into the state of one's vehicle for use by the owner and the owner alone.

This integration will not share user or vehicle data outside Home Assistant, a single-user environment hosted and running within an owner's private network and hardware systems.

![Home Assistant Rivian Card](.github/images/home_assistant_rivian_entity_card.png)

## Installation

1. Use [HACS](https://hacs.xyz/docs/setup/download). In HACS add a custom repository by going to `HACS > Integrations > 3 dots > custom repositories` and adding this github repo `https://github.com/bretterer/home-assistant-rivian`. Now skip to 7.
2. If no HACS, use the tool of choice to open the directory (folder) for your Home Assistant configuration (where you find `configuration.yaml`).
3. If you do not have a `custom_components` directory (folder) there, you need to create it.
4. In the `custom_components` directory (folder) create a new folder called `rivian`.
5. Download _all_ the files from the `custom_components/rivian/` directory (folder) in this repository.
6. Place the files you downloaded in the new directory (folder) you created.
7. Restart Home Assistant.
8. [![Add Integration][add-integration-badge]][add-integration] or in the Home Assistant UI go to "Configuration" -> "Integrations" click "+" and search for "Rivian (Unofficial)".

## Configuration

To connect your newly installed integration to pull available sensor data (see the below example) for your Rivian vehicle, you should follow the following configuration recommendations. Not doing so could put your main Rivian account at risk of being locked and available.

- One Time Password (**recommended**) - It is best security practice always to enable One Time Password / Multi-Factor Authentication. This way, your credentials (username + password) can not be used without your explicit consent through the personal device configured on your Rivian account.
- No Account Reuse* (**recommended**) - Another best security practice is not to reuse accounts or credentials across different integrations (e.g., sharing credentials between Home Assistant and your everyday account). We highly recommend the owner *invite a new driver\*, for example, `user+<purpose>@domain.com`, and use only this profile for configuration within Home Assistant. **Note:** Sensors will not be displayed for this new driver until the driver is signed in to a Rivian phone app and linked to the vehicle like an actual new driver. You may temporarily sign out of your Rivian phone app to set up this driver profile: Profile icon (bottom nav bar) > "Manage account" > Sign out (top right corner).

The configuration flow is as follows:

1. **Username and Password (required)**
   - For the integration to work, we require a username and password to a Rivian account with an active _driver profile_ for the vehicle you wish to monitor remotely.
2. **OTP/MFA (optional)**
   - If OTP is enabled on the account provided, you will receive a text from Rivian with a one-time use passcode entered at this step.

\*Developers and Maintainers of this project take no personal responsibility for misconfiguration or misuse of the code provided by this integration. Account security is entirely the sole responsibility of the owner configuring this integration. Accounts may become locked/quarantined due to this unofficial integration.

## Remote Vehicle Control

In addition to viewing vehicle data, it is also possible to issue certain vehicle commands remotely via Home Assistant as you would in the Rivian mobile app.

**Note:** Gen2 (2025) models are not curently supported for Remote Vehicle Control due to changes in their bluetooth hardware that have not been implemented yet. They are not able to particpate in the secure pairing process required.

To enable this feature, you need to have:

- Home Assistant instance with a Bluetooth adapter (or [ESP32 Bluetooth proxy](https://esphome.io/projects/?type=bluetooth)) with a minimum Bluetooth version of 4.2
  - The Bluetooth adapter/proxy needs to be within reach of your vehicle's Bluetooth radio to complete the one-time, in-vehicle pairing process
  - After pairing, Bluetooth is no longer required as commands are sent remotely via the Rivian cloud
- Two-factor authentication (2FA) enabled on your Rivian account
- An available slot for a phone key (currently limited to 4 per vehicle as of vehicle software 2023.26.00)

To complete the pairing setup, configure the integration and select the vehicle(s) for which you want to enable vehicle controls in Home Assistant and then:

1. From within your vehicle, navigate to "Drivers and Keys" under settings
2. Select "Set Up" for the relevant phone key (this should be the name of your Home Assistant instance's home location, typically "Home")
3. Click on the "Pair" button entity for the associated vehicle in Home Assistant
   - Home Assistant will then attempt to connect to your vehicle and complete the pairing process. On Darwin (MacOS) based systems, a pop-up will appear which you will have to manually click to allow pairing. On Linux or Windows systems, this should happen automatically.

Note: If you are having issues with pairing your vehicle, we recommend investing in an ESP32 with Bluetooth proxy installed as it offers flexibility in placement and has been proven to work where other adapters have not.

## Send a Destination

The `rivian.send_destination` action sends a navigation destination to the vehicle, the same way "Send to vehicle" works in the Rivian app. The destination opens in Trip Details on the center display.

| Field       | Required | Description                                                                                                              |
| ----------- | -------- | ------------------------------------------------------------------------------------------------------------------------ |
| `device_id` | yes      | The vehicle                                                                                                              |
| `location`  | yes      | Text that Rivian looks up, see below                                                                                     |
| `wake`      | no       | Wake the vehicle first if it's asleep (default `true`). Asks Rivian to wake it, and also sends a wake command if vehicle control is set up |
| `replace`   | no       | End the vehicle's current trip first (default `true`). With `false`, the destination is added as a stop ahead of any open trip |
| `wait_for_park` | no   | Wait until the vehicle is in Park before sending (default `true`), so a trip being driven is never replaced |
| `park_timeout`  | no   | Give up with an error if the vehicle isn't in Park within this time (default 2 hours) |
| `not_after`     | no   | Skip the send, without an error, if this time has passed; for example the event's start |

With `replace: true`, a rejected end-trip command stops the send. Navigation commands for the same vehicle run in order, so overlapping automations cannot split an end-trip/send pair.

The action returns `{"result": <int>, "sent": true}` from Rivian, or `{"result": null, "sent": false}` when `not_after` had passed. `result: 0` means Rivian accepted the request, not that the place was found, so check the display when trying a new format.

The `rivian.end_trip` action clears the vehicle's active trip, like the End Trip button on the display. It takes `device_id` and returns `{"success": <bool>, "sequence_number": <int>}`. For a sleeping vehicle, it may stay queued until the vehicle wakes. The server's wake hint alone does not reliably wake it.

Rivian runs a place search on `location`:

- Works: a place name ("Costco Goleta"), a business name with its address, a street address, a Google Maps share link (`https://maps.app.goo.gl/...`)
- Doesn't work: a person's name, alone or with an address (it matches a business instead). Use their street address or a Google Maps share link of it.
- A destination without a business name shows as its street address. A custom label can't be set.

### Plan a Trip

The `rivian.plan_trip` action asks Rivian's trip planner how long a trip from the vehicle's current position would take, charging stops included. Nothing is sent to the vehicle.

| Field            | Required | Description                                                               |
| ---------------- | -------- | ------------------------------------------------------------------------- |
| `device_id`      | yes      | The vehicle                                                               |
| `location`       | yes      | A place name, address, Google Maps share link, or `latitude,longitude`    |
| `departure_time` | no       | When the trip starts (default: now)                                       |

It returns `trip_seconds` (drive plus charging), `drive_seconds`, `charge_seconds`, `distance_meters`, `arrival_soc`, `destination_reachable`, `charging_stops` (name, `charge_seconds`, coordinates) and `destination` (the resolved `name`, `address`, `latitude`, `longitude`). The place lookup is the same Google-backed search the Rivian app uses, so the same formats work as for `send_destination`.

Calls made by a Home Assistant user require access to the selected vehicle: read permission for planning, control permission for sending or ending a trip. These device-wide actions check all Rivian entities on that vehicle. Internal automations without a user context continue to work.

### Calendar to vehicle: setup

One blueprint turns the actions into "send my next calendar event to the vehicle when it's time to leave", with every setting in the Home Assistant UI. Ahead of each calendar event with a location, it plans the trip from the vehicle's position, sends the destination early (if no other event with a location comes first) and again just in time (start minus travel time minus a buffer). Each morning it can also send the day's first event, so long trips are loaded before you're up. Sends wait for the vehicle to be parked and are skipped once the event has started. Drive time comes from Google when configured (traffic-aware), otherwise from Rivian's planner times a traffic factor; charging stops always come from Rivian's planner.

You need: Home Assistant 2026.8 or newer, this integration 1.5.9 or newer, a calendar in Home Assistant, and (optional) the Home Assistant companion app for the leave-by notifications.

**1. Connect your calendar.** Settings → Devices & services → Add integration: [Google Calendar](https://www.home-assistant.io/integrations/google/) for Google, [CalDAV](https://www.home-assistant.io/integrations/caldav/) for iCloud, [Remote Calendar](https://www.home-assistant.io/integrations/remote_calendar/) for an Outlook/ICS feed. Each calendar becomes a `calendar.*` entity. Events need a street address in their location field: pick the place from the calendar app's suggestions, or write the full address including the city.

**2. Find the blueprint.** The integration installs it on startup (1.5.10 and newer) under Settings → Automations & scenes → **Blueprints**, named "Rivian: send calendar events to the vehicle when it's time to leave". On every start the integration writes the bundled copy into `config/blueprints/automation/rivian/`, removes blueprints it no longer ships, and, when a file changed, reloads your automations once Home Assistant has started so they pick up the new version. Don't edit the file there; to customize it, copy it under another folder name in `config/blueprints/automation/`. On older versions, import it from `custom_components/rivian/blueprints/` in this repository.

**3. Create the automation.** On the blueprint, Create automation. Pick the vehicle and your calendars; the other defaults work as they are. If you still have an automation from the retired "first event of the day" blueprint, delete it: the morning send is now a setting here.

For notifications, type your phone's notify action, for example `notify.mobile_app_my_phone` (Developer tools → Actions lists them). Settings:

| Setting | Default | What it does |
| --- | --- | --- |
| Look ahead | 3 h | How long before an event the trip is planned and sent early |
| When to send | Early and just in time | Early loads it right away; just in time sends again at start − travel − buffer, so a trip loaded in between doesn't cost you the destination |
| Traffic factor | 1.5 | Rivian's drive times are free-flow; used when Google isn't set up |
| Google Travel Time | empty | Traffic-aware drive times, see step 4 |
| Buffer | 15 min | Slack included in the send time and the notified leave-by time |
| Include charging time | on | Counts the planner's charging stops |
| Replace the current trip | on | Ends the loaded trip first instead of adding a stop |
| Longest wait for Park | 2 h | Sends never happen while driving; this caps the wait |
| Earliest send time | 06:00 | No sends before this time each day |
| Morning send, Morning send time | on, 06:00 | Also send the day's first event with a location at this time |
| Clear skipped events | off | End the trip if you didn't go: at the event's end, or a set number of minutes from its start (negative = before). Only when the vehicle hasn't moved since the send and no other event with a location is coming up |
| Skip events matching | `://` | Skips meeting links; a regular expression |

**4. Optional: Google for traffic-aware drive times.** Rivian's planner doesn't know about traffic (home to Irvine reads 56 min at any time of day; Google says 2 h at 4:30 PM). Google's Routes API fixes that, at no cost for a few calls a day, but the setup has a few traps:

1. At [console.cloud.google.com](https://console.cloud.google.com), create a project and attach a billing account to it. Google requires one even for free-tier use, and may ask for a prepayment to open it (US$30 in one case). That money sits as credit against usage, which stays at zero at a few calls a day, and Google refunds the unused balance if you close the billing account and ask. A budget alert of $1 is a good safety net.
2. In **that same project**: APIs & Services → Library → enable **Routes API**.
3. APIs & Services → Credentials → Create credentials → API key. Edit the key: Application restrictions **None** (Home Assistant calls from a server; a website or app restriction blocks it), API restrictions → only **Routes API**.
4. Wait a few minutes for the key to propagate, then in Home Assistant add the **Google Travel Time** integration with the key (origin and destination can be anything, e.g. your vehicle's device tracker and `zone.home`).
5. Open the "Time to leave" automation and pick that entry in the Google Travel Time field.

"The Routes API is not enabled for this API key" almost always means the key and the enabled API are in different projects, the key has an application restriction, or it hasn't propagated yet.

**What to expect.** A send that is still waiting for Park when the event starts is skipped. With "Clear skipped events" on, a trip you never drove is ended at the chosen time and you get a "Cleared" notification; if you left late, the vehicle moved, so nothing is cleared. Early and just-in-time sends that become due together are merged into one. If Google returns no route or fails, the Rivian estimate times the traffic factor is used.

The notification reads like "Loaded: Dentist. 1 Main St, Burbank, CA. Leave by 8:38 AM (82 min with traffic)." A Home Assistant restart drops automation runs that are waiting, so an event inside the look-ahead window at restart time is only sent if the restart happens before its trigger. Deleting an event after it was sent doesn't clear the vehicle; the next send replaces it, or call `rivian.end_trip`.

### Example: leave on time for the next calendar event

A minimal example using Rivian's free-flow estimate. Use the blueprint above for traffic estimates, quiet hours, and calendar ordering.

Three hours before each event with a location, plan the trip, wait until it's time to leave (trip time plus a buffer), then send the destination. Any calendar integration works (Google Calendar, CalDAV for iCloud, Remote Calendar for an ICS feed); add one trigger per calendar. The wait runs inside the automation, so a Home Assistant restart during those hours drops it.

```yaml
automation:
  - alias: "Send next event to the truck when it's time to leave"
    mode: parallel
    triggers:
      - trigger: calendar
        event: start
        offset: "-3:00:00"
        entity_id: calendar.personal
    actions:
      - variables:
          buffer_minutes: 15
          location: >-
            {{ trigger.calendar_event.location | default('', true)
               | replace('\n', ', ') | regex_replace('\\s{2,}', ' ') | trim }}
      - condition: template
        # skip events with no location, or a meeting link instead of a place
        value_template: "{{ location != '' and ('://' not in location or 'maps' in location) }}"
      - action: rivian.plan_trip
        data:
          device_id: YOUR_VEHICLE_DEVICE_ID
          location: "{{ location }}"
        response_variable: plan
      - delay:
          seconds: >-
            {{ [0, (as_datetime(trigger.calendar_event.start) - now()).total_seconds()
                   - plan.trip_seconds - buffer_minutes * 60] | max | int }}
      - action: rivian.send_destination
        data:
          device_id: YOUR_VEHICLE_DEVICE_ID
          # the place plan_trip resolved, so the vehicle loads the trip that was planned
          location: "{{ [plan.destination.name, plan.destination.address] | select | join(', ') or location }}"
          wait_for_park: true  # never replace a trip while driving
          not_after: "{{ trigger.calendar_event.start }}"
```

Trips longer than three hours need a longer trigger offset, or the blueprint's morning send.

Put the full street address in the event (including "Blvd", "St" and the city): the place search guesses when parts are missing, and a guess can land on the wrong side of town. The navigation actions work wherever the vehicle is; only the vehicle control entities are limited by the integration's zone setting.

**Caveat:** these use Rivian's `ParseAndShareLocationToVehicle` mutation (documented under "legacy" at [rivian-api.kaedenb.org](https://rivian-api.kaedenb.org/app/legacy/controls/share-location/)) and the app's `SendUpdatedTrip` Parallax command, both unofficial. Rivian could change or remove them at any time.

## Available Sensors

| Name                                                | Domain         | Description                            |
| --------------------------------------------------- | -------------- | -------------------------------------- |
| 12V Battery Health                                  | Sensor         |                                        |
| Active driver                                       | Sensor         | The name of the active driver.         |
| Altitude                                            | Sensor         |                                        |
| Battery Capactiy                                    | Sensor         |                                        |
| Battery State of Charge                             | Sensor         |                                        |
| Battery State of Charge Limit                       | Sensor         |                                        |
| Battery Thermal Runaway Propagation                 | Sensor         |                                        |
| Battery Thermal Status                              | Sensor         |                                        |
| Bearing                                             | Sensor         |                                        |
| Bluetooth Module Failure Status Door Front Left     | Sensor         |                                        |
| Bluetooth Module Failure Status Door Front Right    | Sensor         |                                        |
| Bluetooth Module Failure Status Fascia Front        | Sensor         |                                        |
| Bluetooth Module Failure Status Fascia Rear         | Sensor         |                                        |
| Bluetooth Module Failure Status Instrument Controls | Sensor         |                                        |
| Brake Fluid Level Low                               | Sensor         |                                        |
| Cabin Climate Preconditioning                       | Binary Sensor  | Running/not running status             |
| Cabin Climate Preconditioning Type                  | Sensor         |                                        |
| Cabin Temperature                                   | Sensor         |                                        |
| Car Wash Mode                                       | Binary Sensor  | On/off status                          |
| Charger Connection                                  | Binary Sensor  | Plugged in/unplugged status            |
| Charger Derate Status                               | Sensor         |                                        |
| Charging Status                                     | Binary Sensor  | Charging/not charging status           |
| Charging Schedule Days                              | Sensor         |                                        |
| Charging Time Remaining                             | Sensor         |                                        |
| Closure State                                       | Binary Sensor  | Open/closed status of all closures     |
| Defrost/Defog                                       | Binary Sensor  | Running/not running status             |
| Door Front Left                                     | Binary Sensor  | Open/closed status                     |
| Door Front Left Lock                                | Binary Sensor  | Locked/unlocked status                 |
| Door Front Right                                    | Binary Sensor  | Open/closed status                     |
| Door Front Right Lock                               | Binary Sensor  | Locked/unlocked status                 |
| Door Rear Left                                      | Binary Sensor  | Open/closed status                     |
| Door Rear Left Lock                                 | Binary Sensor  | Locked/unlocked status                 |
| Door Rear Right                                     | Binary Sensor  | Open/closed status                     |
| Door Rear Right Lock                                | Binary Sensor  | Locked/unlocked status                 |
| Door State                                          | Binary Sensor  | Open/closed status of all doors        |
| Drive Mode                                          | Sensor         |                                        |
| Driver Temperature                                  | Sensor         |                                        |
| Estimated Vehicle Range                             | Sensor         |                                        |
| Front Trunk                                         | Binary Sensor  | Open/closed status                     |
| Front Trunk Lock                                    | Binary Sensor  | Locked/unlocked status                 |
| Gear Guard                                          | Binary Sensor  | Locked/unlocked status                 |
| Gear Guard Alarm                                    | Binary Sensor  | Tampered/clear status                  |
| Gear Guard Video Mode                               | Sensor         |                                        |
| Gear Guard Video Status                             | Sensor         |                                        |
| Gear Guard Video Terms Accepted\*                   | Sensor         |                                        |
| Gear Selector                                       | Sensor         |                                        |
| Gear Tunnel Left                                    | Binary Sensor  | Open/closed status, R1T only           |
| Gear Tunnel Left Lock                               | Binary Sensor  | Locked/unlocked status, R1T only       |
| Gear Tunnel Right                                   | Binary Sensor  | Open/closed status, R1T only           |
| Gear Tunnel Right Lock                              | Binary Sensor  | Locked/unlocked status, R1T only       |
| Heated Seat 3rd Row Left                            | Binary Sensor  | Running/not running status, R1S only   |
| Heated Seat 3rd Row Right                           | Binary Sensor  | Running/not running status, R1S only   |
| Heated Seat Front Left                              | Binary Sensor  | Running/not running status             |
| Heated Seat Front Right                             | Binary Sensor  | Running/not running status             |
| Heated Seat Rear Left                               | Binary Sensor  | Running/not running status             |
| Heated Seat Rear Right                              | Binary Sensor  | Running/not running status             |
| Heated Steering Wheel                               | Binary Sensor  | Running/not running status             |
| In Use State                                        | Binary Sensor  | Moving/stopped status                  |
| Liftgate                                            | Binary Sensor  | Open/close status                      |
| Liftgate Lock                                       | Binary Sensor  | Locked/unlocked status                 |
| Liftgate Next Action                                | Sensor         |                                        |
| Limited Acceleration (Cold)                         | Sensor         |                                        |
| Limited Regenerative Braking (Cold)                 | Sensor         |                                        |
| Location                                            | Device Tracker |                                        |
| Locked State                                        | Binary Sensor  | Locked/unlocked status of all closures |
| Odometer                                            | Sensor         |                                        |
| Pet Mode                                            | Binary Sensor  | Running/not running status             |
| Pet Mode Temperature Status                         | Sensor         |                                        |
| Power State                                         | Sensor         |                                        |
| Range Threshold                                     | Sensor         |                                        |
| Remote Charging Available                           | Sensor         |                                        |
| Service Mode                                        | Sensor         |                                        |
| Software                                            | Update         |                                        |
| Software OTA - Available Version\*                  | Sensor         |                                        |
| Software OTA - Available Version Git Hash\*         | Sensor         |                                        |
| Software OTA - Available Version Number\*           | Sensor         |                                        |
| Software OTA - Available Version Week\*             | Sensor         |                                        |
| Software OTA - Available Version Year\*             | Sensor         |                                        |
| Software OTA - Current Version\*                    | Sensor         |                                        |
| Software OTA - Current Version Git Hash\*           | Sensor         |                                        |
| Software OTA - Current Version Number\*             | Sensor         |                                        |
| Software OTA - Current Version Week\*               | Sensor         |                                        |
| Software OTA - Current Version Year\*               | Sensor         |                                        |
| Software OTA - Download Progress                    | Sensor         |                                        |
| Software OTA - Install Duration                     | Sensor         |                                        |
| Software OTA - Install Progress\*                   | Sensor         |                                        |
| Software OTA - Install Ready                        | Sensor         |                                        |
| Software OTA - Install Time                         | Sensor         |                                        |
| Software OTA - Install Type                         | Sensor         |                                        |
| Software OTA - Status                               | Sensor         |                                        |
| Software OTA - Status Current                       | Sensor         |                                        |
| Speed                                               | Sensor         |                                        |
| Tailgate                                            | Binary Sensor  | Open/close status                      |
| Tailgate Lock                                       | Binary Sensor  | Locked/unlocked status                 |
| Tire Pressure Front Left                            | Sensor         |                                        |
| Tire Pressure Front Left Validity                   | Binary Sensor  | OK/problem status                      |
| Tire Pressure Front Right                           | Sensor         |                                        |
| Tire Pressure Front Right Validity                  | Binary Sensor  | OK/problem status                      |
| Tire Pressure Rear Left                             | Sensor         |                                        |
| Tire Pressure Rear Left Validity                    | Binary Sensor  | OK/problem status                      |
| Tire Pressure Rear Right                            | Sensor         |                                        |
| Tire Pressure Rear Right Validity                   | Binary Sensor  | OK/problem status                      |
| Tonneau                                             | Binary Sensor  | Open/close status, R1T only            |
| Tonneau Lock                                        | Binary Sensor  | Locked/unlocked status, R1T only       |
| Trailer Status                                      | Sensor         |                                        |
| Vented Seat Front Left                              | Binary Sensor  | Running/not running status             |
| Vented Seat Front Right                             | Binary Sensor  | Running/not running status             |
| Window Calibration Front Left State                 | Sensor         |                                        |
| Window Calibration Front Right State                | Sensor         |                                        |
| Window Calibration Rear Left State                  | Sensor         |                                        |
| Window Calibration Rear Right State                 | Sensor         |                                        |
| Window Front Left                                   | Binary Sensor  | Open/close status                      |
| Window Front Right                                  | Binary Sensor  | Open/close status                      |
| Window Rear Left                                    | Binary Sensor  | Open/close status                      |
| Window Rear Right                                   | Binary Sensor  | Open/close status                      |
| Windows Next Action                                 | Sensor         |                                        |
| Wiper Fluid Level                                   | Binary Sensor  | OK/problem status                      |

\* Disabled by default

### Remote Vehicle Controls

| Name                           | Domain  | Description                                                                                                                                               |
| ------------------------------ | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alarm                          | Switch  | Sound/mute alarm                                                                                                                                          |
| Cabin Climate                  | Climate | Adjust cabin temperature/preset                                                                                                                           |
| Charge Limit                   | Number  | Set battery charge limit percent                                                                                                                          |
| Charging Enabled               | Switch  | Enable/disable charging while vehicle plugged in and below battery state of charge limit                                                                  |
| Charging Schedule Amperage     | Number  | Set charging schedule amperage limit                                                                                                                      |
| Charging Schedule Enabled      | Switch  | Enable/disable charging schedule                                                                                                                          |
| Charging Schedule End          | Time    | Set charging schedule end time                                                                                                                            |
| Charging Schedule Start        | Time    | Set charging schedule start time                                                                                                                          |
| Closures                       | Lock    | Lock/unlock all closures                                                                                                                                  |
| Drop Tailgate                  | Button  | Drop tailgate                                                                                                                                             |
| Front Trunk                    | Cover   | Open/close front trunk                                                                                                                                    |
| Gear Guard Video               | Switch  | Enable/disable gear guard video                                                                                                                           |
| Liftgate                       | Cover   | Open/close liftgate, R1S only                                                                                                                             |
| Open Gear Tunnel Left          | Button  | Open left gear tunnel                                                                                                                                     |
| Open Gear Tunnel Right         | Button  | Open right gear tunnel                                                                                                                                    |
| Pair                           | Button  | Inititate Bluetooth pairing from Home Assistant. When pairing is identified as completed, this entity will no longer be created and can safely be deleted |
| Seat Front Left Heat           | Select  | Set front left seat heat level                                                                                                                            |
| Seat Front Left Vent           | Select  | Set front right seat vent level                                                                                                                           |
| Seat Front Right Heat          | Select  | Set front right seat heat level                                                                                                                           |
| Seat Front Right Vent          | Select  | Set front right seat vent level                                                                                                                           |
| Seat Rear Left Heat            | Select  | Set rear left seat heat level                                                                                                                             |
| Seat Rear Right Heat           | Select  | Set rear right seat heat level                                                                                                                            |
| Software                       | Update  | Trigger OTA update, if available                                                                                                                          |
| Steering Wheel Heat            | Switch  | Turn on/off steering wheel heat                                                                                                                           |
| Tonneau                        | Cover   | Open/close powered tonneau, R1T only                                                                                                                      |
| Wake                           | Button  | Wake vehicle                                                                                                                                              |
| Windows                        | Cover   | Vent/close all windows                                                                                                                                    |

## Special Thanks

- [jrgutier](https://github.com/jrgutier) - Helped with getting information on the Rivian API
- [tmack8001](https://github.com/tmack8001) - Helping with Development and Testing
- [natekspencer](https://github.com/natekspencer) - Keeping the integration up-to-date with changes from Home Assistant

---

[commits-shield]: https://img.shields.io/github/commit-activity/w/bretterer/home-assistant-rivian?style=flat-square
[commits]: https://github.com/bretterer/home-assistant-rivian/commits/main
[releases-shield]: https://img.shields.io/github/release/bretterer/home-assistant-rivian.svg?style=flat-square
[releases]: https://github.com/bretterer/home-assistant-rivian/releases
[download-all]: https://img.shields.io/github/downloads/bretterer/home-assistant-rivian/total?style=flat-square
[download-latest]: https://img.shields.io/github/downloads/bretterer/home-assistant-rivian/latest/total?style=flat-square
[add-integration]: https://my.home-assistant.io/redirect/config_flow_start?domain=rivian
[add-integration-badge]: https://my.home-assistant.io/badges/config_flow_start.svg
[rivian-discord]: https://discord.gg/jEc5RUPd
