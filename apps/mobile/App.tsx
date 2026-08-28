import { StatusBar } from "expo-status-bar";
import { useState } from "react";
import { Button, StyleSheet, Text, View } from "react-native";

/**
 * Session recorder (MVP): start/stop an audio recording, then upload it to
 * session-service.
 *
 * TODO:
 *  1. Login via Keycloak (expo-auth-session, client `dnd-mobile`) and store
 *     the access token in expo-secure-store.
 *  2. Pick a campaign (GET /api/campaigns), create a session
 *     (POST /api/sessions), then PUT the recording
 *     (PUT /api/sessions/{id}/recording, multipart).
 *  3. Show upload progress + pipeline status (transcribing → identifying
 *     speakers → generating wiki) by polling GET /api/sessions/{id}.
 *  4. "Name this voice" screen: record a 10–30 s sample for voiceprint
 *     enrollment (POST /api/voice/enroll).
 */
export default function App() {
  const [recording, setRecording] = useState(false);
  const [status, setStatus] = useState("Idle — place the phone at the center of the table");

  return (
    <View style={styles.container}>
      <Text style={styles.title}>DnD Wiki Recorder</Text>
      <Text style={styles.status}>{status}</Text>
      <Button
        title={recording ? "Stop & upload" : "Start recording"}
        onPress={() => {
          setRecording((r) => !r);
          setStatus(recording ? "Uploading…" : "Recording…");
        }}
      />
      <StatusBar style="light" />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#020617", alignItems: "center", justifyContent: "center", gap: 24, padding: 24 },
  title: { color: "#f8fafc", fontSize: 24, fontWeight: "700" },
  status: { color: "#94a3b8", fontSize: 14, textAlign: "center" },
});
