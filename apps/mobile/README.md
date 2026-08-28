# apps/mobile — Expo session recorder

The phone app has a deliberately tiny surface: it exists to **capture the
table audio** and enroll voiceprints.

## Screens (planned)

1. **Record** — start/stop; keeps the screen awake; nice big button for table use.
2. **Upload** — create session + PUT multipart recording to session-service;
   shows pipeline progress (transcribing → identifying → generating).
3. **Enroll voiceprint** — record 10–30 s so the platform can recognize you.

## Dev

```bash
npm install
npx expo start
```

Point the app at the gateway: set `EXPO_PUBLIC_API_URL` (e.g.
`http://192.168.x.x:80` — your machine's LAN IP when testing on a real phone).

## TODO

- [ ] Keycloak login (expo-auth-session, client `dnd-mobile`)
- [ ] Recording via `expo-av` (`Audio.Recording.createAsync` with m4a)
- [ ] Multipart upload + progress
- [ ] Voiceprint enrollment flow
- [ ] Token refresh via expo-secure-store
