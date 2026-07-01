import React, { useState, useEffect, useCallback } from "react";
import {
  View,
  Text,
  TextInput,
  FlatList,
  TouchableOpacity,
  StyleSheet,
  Alert,
  ActivityIndicator,
} from "react-native";
import { Stack } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { API_URL } from "../components/config";

type Site = { id: number; name: string; url: string };

export default function SitesScreen() {
  const [sites, setSites] = useState<Site[]>([]);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [crawling, setCrawling] = useState<number | null>(null);

  const fetchSites = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/sites`);
      setSites(await res.json());
    } catch {
      Alert.alert("エラー", "サーバーに接続できません。");
    }
  }, []);

  useEffect(() => { fetchSites(); }, [fetchSites]);

  const addSite = async () => {
    if (!name.trim() || !url.trim()) {
      Alert.alert("入力エラー", "名前とURLを入力してください。");
      return;
    }
    setAdding(true);
    try {
      const res = await fetch(`${API_URL}/sites`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), url: url.trim() }),
      });
      if (res.status === 409) {
        Alert.alert("重複", "このURLはすでに登録されています。");
        return;
      }
      if (!res.ok) throw new Error();
      setName("");
      setUrl("");
      await fetchSites();
    } catch {
      Alert.alert("エラー", "サイトの追加に失敗しました。");
    } finally {
      setAdding(false);
    }
  };

  const deleteSite = (site: Site) => {
    Alert.alert("削除確認", `「${site.name}」を削除しますか？`, [
      { text: "キャンセル", style: "cancel" },
      {
        text: "削除",
        style: "destructive",
        onPress: async () => {
          await fetch(`${API_URL}/sites/${site.id}`, { method: "DELETE" });
          fetchSites();
        },
      },
    ]);
  };

  const crawlSite = async (site: Site) => {
    setCrawling(site.id);
    try {
      const res = await fetch(`${API_URL}/crawl/${site.id}`, { method: "POST" });
      const data = await res.json();
      Alert.alert("クロール完了", `${data.indexed}ページをインデックスしました。`);
    } catch {
      Alert.alert("エラー", "クロールに失敗しました。");
    } finally {
      setCrawling(null);
    }
  };

  return (
    <View style={styles.container}>
      <Stack.Screen options={{ title: "サイト管理" }} />

      {/* Add form */}
      <View style={styles.form}>
        <Text style={styles.formTitle}>検索対象サイトを追加</Text>
        <TextInput
          style={styles.input}
          placeholder="サイト名（例: 公式ドキュメント）"
          placeholderTextColor="#aaa"
          value={name}
          onChangeText={setName}
        />
        <TextInput
          style={styles.input}
          placeholder="URL（例: https://example.com）"
          placeholderTextColor="#aaa"
          value={url}
          onChangeText={setUrl}
          autoCapitalize="none"
          keyboardType="url"
        />
        <TouchableOpacity style={styles.addBtn} onPress={addSite} disabled={adding}>
          {adding ? (
            <ActivityIndicator color="#fff" size="small" />
          ) : (
            <>
              <Ionicons name="add-circle-outline" size={18} color="#fff" />
              <Text style={styles.addBtnText}>追加する</Text>
            </>
          )}
        </TouchableOpacity>
      </View>

      {/* Site list */}
      <Text style={styles.listHeader}>登録済みサイト ({sites.length})</Text>
      <FlatList
        data={sites}
        keyExtractor={(s) => String(s.id)}
        ListEmptyComponent={
          <View style={styles.empty}>
            <Ionicons name="globe-outline" size={44} color="#ccc" />
            <Text style={styles.emptyText}>サイトがまだ登録されていません</Text>
          </View>
        }
        renderItem={({ item }) => (
          <View style={styles.card}>
            <View style={styles.cardBody}>
              <Text style={styles.cardName}>{item.name}</Text>
              <Text style={styles.cardUrl} numberOfLines={1}>{item.url}</Text>
            </View>
            <View style={styles.cardActions}>
              <TouchableOpacity
                style={styles.crawlBtn}
                onPress={() => crawlSite(item)}
                disabled={crawling === item.id}
              >
                {crawling === item.id ? (
                  <ActivityIndicator size="small" color="#4a90d9" />
                ) : (
                  <Ionicons name="refresh-outline" size={18} color="#4a90d9" />
                )}
              </TouchableOpacity>
              <TouchableOpacity onPress={() => deleteSite(item)}>
                <Ionicons name="trash-outline" size={18} color="#e74c3c" />
              </TouchableOpacity>
            </View>
          </View>
        )}
        contentContainerStyle={{ padding: 12, paddingBottom: 40 }}
      />

      <Text style={styles.hint}>
        ↺ ボタン: インデックス検索用にサイトをクロール（事前実行推奨）
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#f0f4f8" },
  form: {
    backgroundColor: "#fff",
    margin: 12,
    borderRadius: 14,
    padding: 16,
    elevation: 2,
    shadowColor: "#000",
    shadowOpacity: 0.07,
    shadowRadius: 4,
    shadowOffset: { width: 0, height: 2 },
  },
  formTitle: { fontSize: 15, fontWeight: "700", color: "#1a1a2e", marginBottom: 12 },
  input: {
    borderWidth: 1,
    borderColor: "#dde3ec",
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 14,
    color: "#333",
    marginBottom: 10,
    backgroundColor: "#f9fbfc",
  },
  addBtn: {
    backgroundColor: "#1a1a2e",
    borderRadius: 10,
    paddingVertical: 12,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
  },
  addBtnText: { color: "#fff", fontWeight: "bold", fontSize: 15 },
  listHeader: {
    paddingHorizontal: 16,
    paddingVertical: 6,
    fontSize: 13,
    fontWeight: "600",
    color: "#666",
    textTransform: "uppercase",
    letterSpacing: 0.5,
  },
  card: {
    backgroundColor: "#fff",
    borderRadius: 12,
    padding: 14,
    marginBottom: 8,
    flexDirection: "row",
    alignItems: "center",
    elevation: 1,
    shadowColor: "#000",
    shadowOpacity: 0.05,
    shadowRadius: 3,
    shadowOffset: { width: 0, height: 1 },
  },
  cardBody: { flex: 1 },
  cardName: { fontSize: 15, fontWeight: "700", color: "#1a1a2e", marginBottom: 3 },
  cardUrl: { fontSize: 12, color: "#888" },
  cardActions: { flexDirection: "row", gap: 16, marginLeft: 12 },
  crawlBtn: { width: 28, alignItems: "center" },
  empty: { alignItems: "center", paddingTop: 40, gap: 10 },
  emptyText: { color: "#bbb", fontSize: 14 },
  hint: {
    textAlign: "center",
    fontSize: 12,
    color: "#aaa",
    paddingHorizontal: 20,
    paddingBottom: 20,
  },
});
