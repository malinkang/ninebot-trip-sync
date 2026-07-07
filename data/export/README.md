# Ninebot Trip Export

## Exported Files

- `trips.csv`: trip summary with start time, end time, duration, mileage, speed, energy, and file names.
- `*_wgs84.gpx`: GPX tracks converted from GCJ-02 to WGS84, suitable for most map/GPS tools.
- `*_gcj02.gpx`: GPX tracks using the raw GCJ-02 coordinates returned by Ninebot/AMap.
- `*_points_gcj02.csv`: point-level coordinates, including raw GCJ-02 and converted WGS84.
- `*.json`: extracted plaintext `travel-info` response data for each trip.

## Discovered Interfaces

The trip module is the React Native `Track` bundle, route `MotorTrackList`.

List endpoint:

```text
POST https://cn-cbu-gateway.ninebot.com/app-api/travel/v6/travel-list2
Content-Type: text/plain
Ninebot-Version: 1
```

Payload shape before native signing/encryption:

```json
{
  "wnumber": "11DFG2532J1219",
  "rnVersion": "753",
  "vehicle_type": "14356",
  "month": "2026-07",
  "page": 1
}
```

Detail endpoint:

```text
POST https://cn-cbu-gateway.ninebot.com/app-api/travel/v6/travel-info
Content-Type: text/plain
Ninebot-Version: 1
```

Payload shape before native signing/encryption:

```json
{
  "wnumber": "11DFG2532J1219",
  "rnVersion": "753",
  "vehicle_type": "14356",
  "travel_id": "<travel_id from travel-list2>",
  "startTime": 1783389230,
  "endTime": 1783390364,
  "businessType": 2
}
```

Important implementation detail: the React Native code calls `NBRequest`, which delegates to the native bridge `tokenRequest(url, data, headers, num2str, withToken, wnumber)`. Directly replaying these endpoints with plain JSON returns an error because the app signs/encrypts the request body in native code before sending it.

Relevant decompiled bundle references:

- `hermes-dec/Track.decompiled.js`: `fetchTravelList`, `fetchTravelInfo`
- `hermes-dec/Track.decompiled.js`: `getTrackListV2`, `getTrackDetail`

## 2026-07-07 Trips

| Start | End | Mileage | Duration | Max Speed | Energy | Points |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2026-07-07 08:11:55 | 2026-07-07 08:15:01 | 0.7 km | 186 s | 32.6 km/h | 10 Wh | 15 |
| 2026-07-07 08:16:47 | 2026-07-07 08:20:57 | 1.2 km | 250 s | 33.9 km/h | 20 Wh | 25 |
| 2026-07-07 09:53:50 | 2026-07-07 10:12:44 | 9.5 km | 1134 s | 38.9 km/h | 130 Wh | 166 |

Total mileage: 11.4 km, matching the app's daily total.
