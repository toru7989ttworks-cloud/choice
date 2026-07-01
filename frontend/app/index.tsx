import React, { useState, useEffect } from "react";
import {
  View,
  Text,
  TextInput,
  FlatList,
  TouchableOpacity,
  StyleSheet,
  ActivityIndicator,
  Linking,
  Switch,
  Alert,
} from "react-native";
import { useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { API_URL } from "../components/config";

type Site = { id: number; name: string; url: string };
type Result = { url: string; title: string; excerpt: string; site_name: string };

export default function HomeScreen() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Result[]>([]);
  const [sites, setSites] = useState<Site[]>([]);
  const [loading, setLoading] = useState(false);
  const [indexedMode, setIndexedMode] = useState(false);
  const [searched, setSearched] = useState(false);

  useEffect(() => {
    fetchSites();
  }, []);

  const fetchSites = async () => {
    try {
      const res = await fetch(`${API_URL}/sites`);
      const data = await res.json();
      setSites(data);
    } catch {
      Alert.alert("エラー", "サーバーに接続できません。バックエンドが起動しているか確認してください。");
    }
  };

  const handleSearch = async () => {
    if (!query.trim()) return;
    if (sites.length === 0) {
      Alert.alert("サイト未登録", "まず検索対象のサイトを追加してください。");
      return;
    }
    setLoading(true);
    setSearched(true);
    try {
      const res = await fetch(`${API_URL}/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, mode: indexedMode ? "indexed" : "realtime" }),
      });
      const data = await res.json();
      setResults(data.results ?? []);
    } catch {
      Alert.alert("エラー", "検索中にエラーが発生しました。");
    } finally {
      setLoading(false);
    }
  };

  const openUrl = (url: string) => Linking.openURL(url);

  return (
    <View style={styles.container}>
      {/* Header */}
      <View style={styles.header}>
        <Text style={styles.headerTitle}>SiteSearch</Text>
        <TouchableOpacity onPress={() => router.push("/sites")} style={styles.headerBtn}>
          <Ionicons name="globe-outline" size={22} color="#fff" />
          <Text style={styles.headerBtnText}>サイト管理 ({sites.length})</Text>
        </TouchableOpacity>
      </View>

      {/* Search bar */}
      <View style={styles.searchBox}>
        <Ionicons name="search" size={20} color="#888" style={{ marginRight: 8 }} />
        <TextInput
          style={styles.input}
          placeholder="キーワードを入力..."
          placeholderTextColor="#aaa"
          value={query}
          onChangeText={setQuery}
          onSubmitEditing={handleSearch}
          returnKeyType="search"
        />
        {query.length > 0 && (
          <TouchableOpacity onPress={() => { setQuery(""); setResults([]); setSearched(false); }}>
            <Ionicons name="close-circle" size={20} color="#aaa" />
          </TouchableOpacity>
        )}
      </View>

      {/* Mode toggle */}
      <View style={styles.modeRow}>
        <Text style={styles.modeLabel}>リアルタイム</Text>
        <Switch
          value={indexedMode}
          onValueChange={setIndexedMode}
          trackColor={{ false: "#4a90d9", true: "#e27d60" }}
          thumbColor="#fff"
        />
        <Text style={styles.modeLabel}>インデックス</Text>
        <TouchableOpacity onPress={handleSearch} style={styles.searchBtn} disabled={loading}>
          <Text style={styles.searchBtnText}>検索</Text>
        </TouchableOpacity>
      </View>

      {/* Results */}
      {loading ? (
        <View style={styles.center}>
          <ActivityIndicator size="large" color="#1a1a2e" />
          <Text style={styles.loadingText}>検索中...</Text>
        </View>
      ) : (
        <FlatList
          data={results}
          keyExtractor={(_, i) => String(i)}
          ListEmptyComponent={
            searched ? (
              <View style={styles.center}>
                <Ionicons name="search-outline" size={48} color="#ccc" />
                <Text style={styles.emptyText}>結果が見つかりませんでした</Text>
              </View>
            ) : (
              <View style={styles.center}>
                <Ionicons name="globe-outline" size={48} color="#ccc" />
                <Text style={styles.emptyText}>
                  {sites.length === 0
                    ? "まずサイトを追加してください"
                    : `${sites.length}件のサイトから検索できます`}
                </Text>
              </View>
            )
          }
          renderItem={({ item }) => (
            <TouchableOpacity style={styles.card} onPress={() => openUrl(item.url)}>
              <Text style={styles.cardSite}>{item.site_name}</Text>
              <Text style={styles.cardTitle} numberOfLines={2}>{item.title}</Text>
              <Text style={styles.cardExcerpt} numberOfLines={3}>{item.excerpt}</Text>
              <Text style={styles.cardUrl} numberOfLines={1}>{item.url}</Text>
            </TouchableOpacity>
          )}
          contentContainerStyle={{ padding: 12, paddingBottom: 40 }}
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#f0f4f8" },
  header: {
    backgroundColor: "#1a1a2e",
    paddingTop: 50,
    paddingBottom: 14,
    paddingHorizontal: 16,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  headerTitle: { color: "#fff", fontSize: 22, fontWeight: "bold" },
  headerBtn: { flexDirection: "row", alignItems: "center", gap: 4 },
  headerBtnText: { color: "#ccc", fontSize: 13 },
  searchBox: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: "#fff",
    margin: 12,
    borderRadius: 12,
    paddingHorizontal: 14,
    paddingVertical: 10,
    elevation: 2,
    shadowColor: "#000",
    shadowOpacity: 0.08,
    shadowRadius: 4,
    shadowOffset: { width: 0, height: 2 },
  },
  input: { flex: 1, fontSize: 16, color: "#333" },
  modeRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 16,
    marginBottom: 4,
    gap: 8,
  },
  modeLabel: { color: "#555", fontSize: 13 },
  searchBtn: {
    marginLeft: "auto",
    backgroundColor: "#1a1a2e",
    paddingHorizontal: 20,
    paddingVertical: 8,
    borderRadius: 20,
  },
  searchBtnText: { color: "#fff", fontWeight: "bold", fontSize: 14 },
  card: {
    backgroundColor: "#fff",
    borderRadius: 12,
    padding: 14,
    marginBottom: 10,
    elevation: 2,
    shadowColor: "#000",
    shadowOpacity: 0.06,
    shadowRadius: 4,
    shadowOffset: { width: 0, height: 2 },
  },
  cardSite: { color: "#4a90d9", fontSize: 11, fontWeight: "600", marginBottom: 4, textTransform: "uppercase" },
  cardTitle: { fontSize: 16, fontWeight: "bold", color: "#1a1a2e", marginBottom: 6 },
  cardExcerpt: { fontSize: 13, color: "#555", lineHeight: 19, marginBottom: 6 },
  cardUrl: { fontSize: 11, color: "#aaa" },
  center: { alignItems: "center", justifyContent: "center", paddingTop: 60, gap: 12 },
  emptyText: { color: "#aaa", fontSize: 15, textAlign: "center" },
  loadingText: { color: "#888", marginTop: 8 },
});
