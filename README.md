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

![Home Assistant Rivian Card](.github/images/home_assistant_rivian_entity_card.jpg)

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

**Note:** Remote vehicle control isn't supported yet for Gen2 R1T and R1S vehicles (2025 model year and later) or the R2. Their Bluetooth hardware changed, and how they complete the secure pairing process hasn't been figured out yet.

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

One blueprint turns the actions into "send my next calendar event to the vehicle when it's time to leave", with every setting in the Home Assistant UI. Ahead of each calendar event with a location, it plans the trip from the vehicle's position, sends the destination early (if no other event with a location comes first) and again just in time (start minus travel time minus a buffer). Each morning it can also send the day's first event, so long trips are loaded before you're up; once that morning send has gone out, the early send is skipped so the same destination doesn't land twice. Sends wait for the vehicle to be parked and are skipped once the event has started. Drive time comes from Google when configured (traffic-aware), otherwise from Rivian's planner times a traffic factor; charging stops always come from Rivian's planner.

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
| Early send after the morning send | off | The early send is skipped once the morning send time has passed, since the morning send already loaded the day's first event. Turn on to send early anyway, for events added after the morning send |
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

The notification reads like "Loaded: Dentist. 1 Main St, Burbank, CA. Leave by 8:38 AM (82 min with traffic)." A Home Assistant restart (including a core update) cancels automation runs that are waiting; on start, the blueprint looks for an event with a location due within the look-ahead and runs the flow for it again, so the destination is re-sent and the just-in-time send re-armed. Deleting an event after it was sent doesn't clear the vehicle; the next send replaces it, or call `rivian.end_trip`.

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

Entities are available for all models (R1T, R1S and R2) unless noted otherwise.

| Name                                                | Domain         | Description                                                                                                                    |
| --------------------------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| 12V battery health                                  | Sensor         |                                                                                                                                |
| Active driver                                       | Sensor         | The name of the active driver.                                                                                                 |
| Altitude                                            | Sensor         |                                                                                                                                |
| Amperage                                            | Sensor         | Wallbox current                                                                                                                |
| Amperage maximum                                    | Sensor         | Wallbox maximum current                                                                                                        |
| Battery capacity                                    | Sensor         |                                                                                                                                |
| Battery state of charge                             | Sensor         |                                                                                                                                |
| Battery state of charge limit                       | Sensor         |                                                                                                                                |
| Battery thermal runaway propagation                 | Sensor         |                                                                                                                                |
| Battery thermal status                              | Sensor         |                                                                                                                                |
| Bearing                                             | Sensor         |                                                                                                                                |
| Bluetooth module failure status door front left     | Sensor         |                                                                                                                                |
| Bluetooth module failure status door front right    | Sensor         |                                                                                                                                |
| Bluetooth module failure status fascia front        | Sensor         |                                                                                                                                |
| Bluetooth module failure status fascia rear         | Sensor         |                                                                                                                                |
| Bluetooth module failure status instrument controls | Sensor         |                                                                                                                                |
| Bluetooth module failure status overhead console    | Sensor         |                                                                                                                                |
| Brake fluid level                                   | Sensor         |                                                                                                                                |
| Cabin climate preconditioning                       | Binary sensor  | Running/not running status                                                                                                     |
| Cabin climate preconditioning type                  | Sensor         |                                                                                                                                |
| Cabin temperature                                   | Sensor         |                                                                                                                                |
| Car wash mode                                       | Binary sensor  | On/off status                                                                                                                  |
| Cellular carrier                                    | Sensor         |                                                                                                                                |
| Cellular mode                                       | Sensor         |                                                                                                                                |
| Cellular signal bars                                | Sensor         |                                                                                                                                |
| Cellular signal strength                            | Sensor         |                                                                                                                                |
| Charge port                                         | Binary sensor  | Open/closed status, R1T and R1S only                                                                                           |
| Charge port door next action                        | Sensor         | R1T and R1S only                                                                                                               |
| Charger connection                                  | Binary sensor  | Plugged in/unplugged status                                                                                                    |
| Charger derate status                               | Sensor         |                                                                                                                                |
| Charger state                                       | Sensor         | Detailed charging state: charging, scheduled, complete, waiting on charger, errors, etc.                                       |
| Charging cost                                       | Sensor         | Cost of the current or last charging session                                                                                   |
| Charging energy delivered                           | Sensor         | Energy delivered in the current or last charging session                                                                       |
| Charging fault                                      | Sensor         | Why charging is disabled or reduced, if it is                                                                                  |
| Charging range added                                | Sensor         | Range added in the current or last charging session                                                                            |
| Charging rate                                       | Sensor         | Range added per hour while charging                                                                                            |
| Charging schedule days                              | Sensor         |                                                                                                                                |
| Charging speed                                      | Sensor         | Charging power                                                                                                                 |
| Charging start time                                 | Sensor         | Start of the current or last charging session                                                                                  |
| Charging status                                     | Binary sensor  | Charging/not charging status                                                                                                   |
| Charging status                                     | Sensor         | Wallbox status: available, plugged in, charging, etc.                                                                          |
| Charging time elapsed                               | Sensor         | Time spent actively charging in the current or last session                                                                    |
| Charging time remaining                             | Sensor         |                                                                                                                                |
| Charging time remaining validity                    | Sensor         | Whether the charging time remaining is valid, or the battery is discharging                                                    |
| Closure state                                       | Binary sensor  | Open/closed status of all closures                                                                                             |
| Cold range notification                             | Sensor         | Whether cold weather is reducing range                                                                                         |
| Defrost/defog                                       | Binary sensor  | Running/not running status                                                                                                     |
| Destination                                         | Sensor         | Final stop of the active navigation trip                                                                                       |
| Destination ETA                                     | Sensor         | Estimated arrival at the destination                                                                                           |
| Door front left                                     | Binary sensor  | Open/closed status                                                                                                             |
| Door front left lock                                | Binary sensor  | Locked/unlocked status                                                                                                         |
| Door front right                                    | Binary sensor  | Open/closed status                                                                                                             |
| Door front right lock                               | Binary sensor  | Locked/unlocked status                                                                                                         |
| Door rear left                                      | Binary sensor  | Open/closed status                                                                                                             |
| Door rear left lock                                 | Binary sensor  | Locked/unlocked status                                                                                                         |
| Door rear right                                     | Binary sensor  | Open/closed status                                                                                                             |
| Door rear right lock                                | Binary sensor  | Locked/unlocked status                                                                                                         |
| Door state                                          | Binary sensor  | Open/closed status of all doors                                                                                                |
| Drive mode                                          | Sensor         |                                                                                                                                |
| Driver temperature                                  | Sensor         |                                                                                                                                |
| Drivers                                             | Sensor         | Number of drivers with access to the vehicle                                                                                   |
| Estimated vehicle range                             | Sensor         |                                                                                                                                |
| Front trunk                                         | Binary sensor  | Open/closed status                                                                                                             |
| Front trunk lock                                    | Binary sensor  | Locked/unlocked status                                                                                                         |
| Front trunk next action                             | Sensor         |                                                                                                                                |
| Gear Guard                                          | Binary sensor  | Locked/unlocked status, R1T and R1S only                                                                                       |
| Gear Guard alarm                                    | Binary sensor  | Tampered/clear status                                                                                                          |
| Gear Guard video mode                               | Sensor         |                                                                                                                                |
| Gear Guard video status                             | Sensor         |                                                                                                                                |
| Gear Guard video terms accepted\*                   | Sensor         |                                                                                                                                |
| Gear selector                                       | Sensor         |                                                                                                                                |
| Gear tunnel left                                    | Binary sensor  | Open/closed status, R1T only                                                                                                   |
| Gear tunnel left lock                               | Binary sensor  | Locked/unlocked status, R1T only                                                                                               |
| Gear tunnel left next action                        | Sensor         | R1T only                                                                                                                       |
| Gear tunnel right                                   | Binary sensor  | Open/closed status, R1T only                                                                                                   |
| Gear tunnel right lock                              | Binary sensor  | Locked/unlocked status, R1T only                                                                                               |
| Gear tunnel right next action                       | Sensor         | R1T only                                                                                                                       |
| Heated seat 3rd row left                            | Binary sensor  | Running/not running status, R1S only                                                                                           |
| Heated seat 3rd row right                           | Binary sensor  | Running/not running status, R1S only                                                                                           |
| Heated seat front left                              | Binary sensor  | Running/not running status                                                                                                     |
| Heated seat front right                             | Binary sensor  | Running/not running status                                                                                                     |
| Heated seat rear left                               | Binary sensor  | Running/not running status                                                                                                     |
| Heated seat rear right                              | Binary sensor  | Running/not running status                                                                                                     |
| Heated steering wheel                               | Binary sensor  | Running/not running status                                                                                                     |
| In use state                                        | Binary sensor  | Moving/stopped status                                                                                                          |
| Keys                                                | Sensor         | Number of phone keys across all drivers                                                                                        |
| Liftgate                                            | Binary sensor  | Open/close status, R1S and R2 only                                                                                             |
| Liftgate lock                                       | Binary sensor  | Locked/unlocked status, R1S and R2 only                                                                                        |
| Liftgate next action                                | Sensor         | R1S and R2 only                                                                                                                |
| Limited acceleration (cold)                         | Sensor         |                                                                                                                                |
| Limited regenerative braking (cold)                 | Sensor         |                                                                                                                                |
| Locked state                                        | Binary sensor  | Locked/unlocked status of all closures                                                                                         |
| Next stop                                           | Sensor         | Next stop of the active navigation trip, including charging stops, past the one the vehicle is at (its `arrived_at` attribute) |
| Next stop distance                                  | Sensor         | Distance left to the next stop                                                                                                 |
| Next stop drive time                                | Sensor         | Driving time left to the next stop                                                                                             |
| Next stop ETA                                       | Sensor         | Estimated arrival at the next stop                                                                                             |
| No updates since                                    | Sensor         | When the vehicle last reported data; empty unless that was over 10 minutes ago                                                 |
| Odometer                                            | Sensor         |                                                                                                                                |
| Pet mode                                            | Binary sensor  | Running/not running status                                                                                                     |
| Pet mode temperature status                         | Sensor         |                                                                                                                                |
| Power                                               | Sensor         | Wallbox power                                                                                                                  |
| Power maximum                                       | Sensor         | Wallbox maximum power                                                                                                          |
| Power state                                         | Sensor         |                                                                                                                                |
| Range threshold                                     | Sensor         |                                                                                                                                |
| Rear hitch status                                   | Sensor         | Nothing, an accessory or a trailer on the hitch                                                                                |
| Remote charging available                           | Sensor         |                                                                                                                                |
| Service mode                                        | Sensor         |                                                                                                                                |
| Software                                            | Update         |                                                                                                                                |
| Software OTA - Download progress                    | Sensor         |                                                                                                                                |
| Software OTA - Install duration                     | Sensor         |                                                                                                                                |
| Software OTA - Install ready                        | Sensor         |                                                                                                                                |
| Software OTA - Install time                         | Sensor         |                                                                                                                                |
| Software OTA - Install type                         | Sensor         | R1T and R1S only                                                                                                               |
| Software OTA - Last install result                  | Sensor         |                                                                                                                                |
| Software OTA - Status                               | Sensor         |                                                                                                                                |
| Speed                                               | Sensor         |                                                                                                                                |
| Tailgate                                            | Binary sensor  | Open/close status, R1T only                                                                                                    |
| Tailgate lock                                       | Binary sensor  | Locked/unlocked status, R1T and R1S only                                                                                       |
| Tailgate next action                                | Sensor         | R1T only                                                                                                                       |
| Tire pressure front left                            | Sensor         |                                                                                                                                |
| Tire pressure front left status                     | Sensor         | OK/warning status                                                                                                              |
| Tire pressure front left validity                   | Binary sensor  | OK/problem status                                                                                                              |
| Tire pressure front right                           | Sensor         |                                                                                                                                |
| Tire pressure front right status                    | Sensor         | OK/warning status                                                                                                              |
| Tire pressure front right validity                  | Binary sensor  | OK/problem status                                                                                                              |
| Tire pressure rear left                             | Sensor         |                                                                                                                                |
| Tire pressure rear left status                      | Sensor         | OK/warning status                                                                                                              |
| Tire pressure rear left validity                    | Binary sensor  | OK/problem status                                                                                                              |
| Tire pressure rear right                            | Sensor         |                                                                                                                                |
| Tire pressure rear right status                     | Sensor         | OK/warning status                                                                                                              |
| Tire pressure rear right validity                   | Binary sensor  | OK/problem status                                                                                                              |
| Tonneau                                             | Binary sensor  | Open/close status, R1T with a powered tonneau only                                                                             |
| Tonneau lock                                        | Binary sensor  | Locked/unlocked status, R1T with a powered tonneau only                                                                        |
| Trailer status                                      | Sensor         |                                                                                                                                |
| _Vehicle name_                                      | Device tracker | Vehicle location, named after the vehicle itself                                                                               |
| Vehicle updates                                     | Binary sensor  | OK/problem status: a problem when nothing has arrived from Rivian for 10 minutes                                               |
| Vented seat front left                              | Binary sensor  | Running/not running status                                                                                                     |
| Vented seat front right                             | Binary sensor  | Running/not running status                                                                                                     |
| Voltage                                             | Sensor         | Wallbox voltage                                                                                                                |
| Voltage maximum                                     | Sensor         | Wallbox maximum voltage                                                                                                        |
| Wi-Fi connection status                             | Sensor         |                                                                                                                                |
| Wi-Fi frequency                                     | Sensor         |                                                                                                                                |
| Wi-Fi link speed                                    | Sensor         |                                                                                                                                |
| Wi-Fi network\*                                     | Sensor         | Network name (SSID)                                                                                                            |
| Wi-Fi security                                      | Sensor         |                                                                                                                                |
| Wi-Fi signal bars                                   | Sensor         |                                                                                                                                |
| Wi-Fi signal strength                               | Sensor         |                                                                                                                                |
| Window calibration front left state                 | Sensor         |                                                                                                                                |
| Window calibration front right state                | Sensor         |                                                                                                                                |
| Window calibration liftgate state                   | Sensor         | R2 only                                                                                                                        |
| Window calibration rear left state                  | Sensor         |                                                                                                                                |
| Window calibration rear right state                 | Sensor         |                                                                                                                                |
| Window front left                                   | Binary sensor  | Open/close status                                                                                                              |
| Window front right                                  | Binary sensor  | Open/close status                                                                                                              |
| Window liftgate                                     | Binary sensor  | Open/closed status, R2 only                                                                                                    |
| Window rear left                                    | Binary sensor  | Open/close status                                                                                                              |
| Window rear right                                   | Binary sensor  | Open/close status                                                                                                              |
| Windows next action                                 | Sensor         |                                                                                                                                |
| Wiper fluid level                                   | Binary sensor  | OK/problem status                                                                                                              |

\* Disabled by default

### Remote Vehicle Controls

These controls need a phone key paired over Bluetooth (see [Remote Vehicle Control](#remote-vehicle-control)), which isn't possible yet for Gen2 R1T and R1S vehicles or the R2. Otherwise, they're available for all models unless noted, and some also depend on the features your vehicle reports.

| Name                       | Domain  | Description                                                                                                                                              |
| -------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alarm                      | Switch  | Sound/mute alarm                                                                                                                                         |
| Cabin climate              | Climate | Adjust cabin temperature/preset                                                                                                                          |
| Charge limit               | Number  | Set battery charge limit percent                                                                                                                         |
| Charge port door           | Cover   | Open/close the powered charge port door, R1T and R1S only                                                                                                |
| Charging enabled           | Switch  | Enable/disable charging while vehicle plugged in and below battery state of charge limit                                                                 |
| Charging schedule amperage | Number  | Set charging schedule amperage limit                                                                                                                     |
| Charging schedule enabled  | Switch  | Enable/disable charging schedule                                                                                                                         |
| Charging schedule end      | Time    | Set charging schedule end time                                                                                                                           |
| Charging schedule start    | Time    | Set charging schedule start time                                                                                                                         |
| Closures                   | Lock    | Lock/unlock all closures                                                                                                                                 |
| Drop tailgate              | Button  | Drop tailgate, R1T only                                                                                                                                  |
| Front trunk                | Cover   | Open/close front trunk                                                                                                                                   |
| Gear Guard video           | Switch  | Enable/disable gear guard video                                                                                                                          |
| Liftgate                   | Cover   | Open/close liftgate, R1S and R2 only                                                                                                                     |
| Open gear tunnel left      | Button  | Open left gear tunnel, R1T only                                                                                                                          |
| Open gear tunnel right     | Button  | Open right gear tunnel, R1T only                                                                                                                         |
| Pair                       | Button  | Initiate Bluetooth pairing from Home Assistant. When pairing is identified as completed, this entity will no longer be created and can safely be deleted |
| Seat front left heat       | Select  | Set front left seat heat level                                                                                                                           |
| Seat front left vent       | Select  | Set front left seat vent level                                                                                                                           |
| Seat front right heat      | Select  | Set front right seat heat level                                                                                                                          |
| Seat front right vent      | Select  | Set front right seat vent level                                                                                                                          |
| Seat rear left heat        | Select  | Set rear left seat heat level                                                                                                                            |
| Seat rear right heat       | Select  | Set rear right seat heat level                                                                                                                           |
| Software                   | Update  | Trigger OTA update, if available                                                                                                                         |
| Steering wheel heat        | Switch  | Turn on/off steering wheel heat                                                                                                                          |
| Tonneau                    | Cover   | Open/close tonneau, R1T with a powered tonneau only                                                                                                      |
| Wake                       | Button  | Wake vehicle                                                                                                                                             |
| Windows                    | Cover   | Vent/close all windows                                                                                                                                   |

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

## Trademark Legal Notices

All product names, trademarks and registered trademarks in the images in this
repository, are property of their respective owners. All images in this
repository are used by the Home Assistant project for identification purposes
only.

The use of these names, trademarks and brands appearing in these image files,
do not imply endorsement.
