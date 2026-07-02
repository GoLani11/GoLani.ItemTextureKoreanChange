using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Threading.Tasks;
using BepInEx;
using BepInEx.Configuration;
using HarmonyLib;
using Newtonsoft.Json;
using UnityEngine;

[BepInPlugin("com.golani.hiresinspect", "GoLani HiRes Inspect", "1.0.0")]
public class Plugin : BaseUnityPlugin
{
    private static Plugin _instance;

    private readonly Dictionary<string, Entry> _manifest = new Dictionary<string, Entry>(StringComparer.Ordinal);
    private readonly Dictionary<string, CacheItem> _cache = new Dictionary<string, CacheItem>(StringComparer.Ordinal);
    private readonly Dictionary<string, Task<LoadResult>> _loadTasks = new Dictionary<string, Task<LoadResult>>(StringComparer.Ordinal);
    private readonly Dictionary<string, List<PendingSwap>> _pendingSwapsByName = new Dictionary<string, List<PendingSwap>>(StringComparer.Ordinal);
    private readonly List<Swap> _swaps = new List<Swap>();
    private readonly HashSet<string> _activeSwapKeys = new HashSet<string>(StringComparer.Ordinal);

    private Harmony _harmony;
    private string _hiresDir;
    private Coroutine _scanRoutine;
    private Coroutine _applyRoutine;
    private ConfigEntry<int> _cacheBudgetMb;
    private int _generation;
    private long _cacheBytes;
    private long _lruCounter;

    private void Awake()
    {
        _instance = this;

        try
        {
            _cacheBudgetMb = Config.Bind(
                "General",
                "CacheBudgetMB",
                256,
                "하이레즈 텍스처 VRAM 캐시 예산(MB). 0이면 닫을 때 즉시 해제");

            if (!LoadManifest())
            {
                return;
            }

            _harmony = new Harmony("com.golani.hiresinspect");
            PatchInspectPanel();
        }
        catch (Exception e)
        {
            Logger.LogError($"하이레즈 inspect 플러그인 초기화 실패: {e}");
        }
    }

    private void OnDestroy()
    {
        try
        {
            if (_scanRoutine != null)
            {
                StopCoroutine(_scanRoutine);
                _scanRoutine = null;
            }

            if (_applyRoutine != null)
            {
                StopCoroutine(_applyRoutine);
                _applyRoutine = null;
            }

            Restore();
            var destroyed = DestroyAllCachedTextures();
            _loadTasks.Clear();
            if (destroyed > 0)
            {
                Resources.UnloadUnusedAssets();
                Logger.LogInfo($"하이레즈 캐시 전체 해제: 텍스처 {destroyed}개");
            }

            if (_harmony != null)
            {
                _harmony.UnpatchSelf();
            }
        }
        catch (Exception e)
        {
            Logger.LogError($"하이레즈 inspect 종료 처리 실패: {e}");
        }
    }

    private bool LoadManifest()
    {
        var pluginDir = Path.GetDirectoryName(Info.Location);
        if (string.IsNullOrEmpty(pluginDir))
        {
            Logger.LogWarning("플러그인 위치를 찾지 못해 하이레즈 inspect를 비활성화합니다.");
            return false;
        }

        _hiresDir = Path.Combine(pluginDir, "hires");
        var manifestPath = Path.Combine(_hiresDir, "manifest.json");
        if (!File.Exists(manifestPath))
        {
            Logger.LogWarning($"하이레즈 매니페스트 없음: {manifestPath}");
            return false;
        }

        var json = File.ReadAllText(manifestPath);
        var entries = JsonConvert.DeserializeObject<List<Entry>>(json);
        if (entries == null || entries.Count == 0)
        {
            Logger.LogWarning("하이레즈 매니페스트가 비어 있습니다.");
            return false;
        }

        foreach (var entry in entries)
        {
            if (entry == null || string.IsNullOrEmpty(entry.Name) || string.IsNullOrEmpty(entry.File))
            {
                continue;
            }

            _manifest[entry.Name] = entry;
        }

        if (_manifest.Count == 0)
        {
            Logger.LogWarning("사용 가능한 하이레즈 텍스처 항목이 없습니다.");
            return false;
        }

        Logger.LogInfo($"하이레즈 매니페스트 {_manifest.Count}개 로드");
        return true;
    }

    private void PatchInspectPanel()
    {
        var targetType = AccessTools.TypeByName("EFT.UI.ItemSpecificationPanel");
        if (targetType == null)
        {
            Logger.LogError("EFT.UI.ItemSpecificationPanel 타입을 찾지 못했습니다.");
            DumpSpecificationTypes();
            return;
        }

        var showPostfix = AccessTools.Method(typeof(Plugin), nameof(OnInspectShown));
        var closePostfix = AccessTools.Method(typeof(Plugin), nameof(OnInspectClosed));

        var showCount = PatchMethodsByName(targetType, "Show", showPostfix);
        if (showCount == 0)
        {
            Logger.LogError("ItemSpecificationPanel.Show 오버로드를 찾지 못했습니다.");
        }

        var closeCount = PatchMethodsByName(targetType, "Close", closePostfix);
        if (closeCount == 0)
        {
            Logger.LogWarning("ItemSpecificationPanel.Close 없음. OnClose를 시도합니다.");
            closeCount = PatchMethodsByName(targetType, "OnClose", closePostfix);
        }

        if (closeCount == 0)
        {
            Logger.LogError("ItemSpecificationPanel.Close/OnClose 오버로드를 찾지 못했습니다.");
            DumpMethods(targetType);
        }
    }

    private int PatchMethodsByName(Type targetType, string methodName, MethodInfo postfix)
    {
        var count = 0;
        var methods = targetType.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
        foreach (var method in methods.Where(m => m.Name == methodName))
        {
            try
            {
                if (method.IsGenericMethodDefinition || method.ContainsGenericParameters)
                {
                    Logger.LogWarning($"제네릭 메서드 스킵: {FormatMethod(method)}");
                    continue;
                }

                _harmony.Patch(method, postfix: new HarmonyMethod(postfix));
                count++;
                Logger.LogInfo($"패치 성공: {FormatMethod(method)}");
            }
            catch (Exception e)
            {
                Logger.LogError($"패치 실패: {FormatMethod(method)}\n{e}");
            }
        }

        return count;
    }

    private static void OnInspectShown(object __instance)
    {
        try
        {
            if (_instance != null)
            {
                // __instance = 열린 inspect 패널. 이 하위만 스캔하면 레이드 전체 렌더러 스캔(수만 개)을 피함.
                _instance.HandleInspectShown(__instance as Component);
            }
        }
        catch (Exception e)
        {
            if (_instance != null)
            {
                _instance.Logger.LogError($"inspect 열림 처리 실패: {e}");
            }
        }
    }

    private static void OnInspectClosed()
    {
        try
        {
            if (_instance != null)
            {
                _instance.HandleInspectClosed();
            }
        }
        catch (Exception e)
        {
            if (_instance != null)
            {
                _instance.Logger.LogError($"inspect 닫힘 처리 실패: {e}");
            }
        }
    }

    private void HandleInspectShown(Component panel)
    {
        try
        {
            if (_scanRoutine != null)
            {
                StopCoroutine(_scanRoutine);
                _scanRoutine = null;
            }

            if (_swaps.Count > 0 || _activeSwapKeys.Count > 0 || _pendingSwapsByName.Count > 0)
            {
                Restore();
            }

            _scanRoutine = StartCoroutine(ScanAfterInspectShown(panel));
        }
        catch (Exception e)
        {
            Logger.LogError($"inspect 열림 코루틴 시작 실패: {e}");
        }
    }

    private void HandleInspectClosed()
    {
        try
        {
            if (_scanRoutine != null)
            {
                StopCoroutine(_scanRoutine);
                _scanRoutine = null;
            }

            Restore();
        }
        catch (Exception e)
        {
            Logger.LogError($"inspect 닫힘 처리 실패: {e}");
        }
    }

    private IEnumerator ScanAfterInspectShown(Component panel)
    {
        yield return null;
        ScanRenderers(panel, false);  // 1프레임째: 모델이 아직 안 붙었을 수 있으니 전체 스캔 폴백 금지
        yield return new WaitForSeconds(0.5f);
        ScanRenderers(panel, true);   // 0.5초 후에도 패널 하위에 없으면 그때만 전체 스캔 폴백
        _scanRoutine = null;
    }

    private void ScanRenderers(Component panel, bool allowGlobalFallback)
    {
        try
        {
            // 패널 하위만 스캔(값싸다). 못 찾으면(마지막 패스에서만) 전체 씬 스캔 폴백(구버전 동작 보존).
            var matched = MatchRenderers(GetPanelRenderers(panel));
            if (matched == 0 && panel != null && allowGlobalFallback)
            {
                matched = MatchRenderers(UnityEngine.Object.FindObjectsOfType<Renderer>());
            }

            Logger.LogInfo($"하이레즈 스캔 완료: 매칭 {matched}개");
        }
        catch (Exception e)
        {
            Logger.LogError($"하이레즈 스캔 실패: {e}");
        }
    }

    private Renderer[] GetPanelRenderers(Component panel)
    {
        if (panel == null)
        {
            return UnityEngine.Object.FindObjectsOfType<Renderer>();
        }

        try
        {
            return panel.GetComponentsInChildren<Renderer>(true);
        }
        catch (Exception e)
        {
            Logger.LogError($"패널 렌더러 수집 실패: {e}");
            return UnityEngine.Object.FindObjectsOfType<Renderer>();
        }
    }

    private int MatchRenderers(Renderer[] renderers)
    {
        var names = new HashSet<string>(StringComparer.Ordinal);
        var pendingSwaps = new List<PendingSwap>();
        {
            foreach (var renderer in renderers)
            {
                if (renderer == null)
                {
                    continue;
                }

                Material[] shared;
                try
                {
                    shared = renderer.sharedMaterials;
                }
                catch (Exception e)
                {
                    Logger.LogError($"sharedMaterials 읽기 실패: {e}");
                    continue;
                }

                if (shared == null)
                {
                    continue;
                }

                for (var i = 0; i < shared.Length; i++)
                {
                    var material = shared[i];
                    if (material == null)
                    {
                        continue;
                    }

                    string[] props;
                    try
                    {
                        props = material.GetTexturePropertyNames();
                    }
                    catch (Exception e)
                    {
                        Logger.LogError($"텍스처 프로퍼티 목록 읽기 실패: {e}");
                        continue;
                    }

                    foreach (var prop in props)
                    {
                        var swapKey = MakeSwapKey(renderer, i, prop);
                        if (_activeSwapKeys.Contains(swapKey))
                        {
                            continue;
                        }

                        Texture original;
                        try
                        {
                            original = material.GetTexture(prop);
                        }
                        catch (Exception e)
                        {
                            Logger.LogError($"텍스처 읽기 실패: {prop}\n{e}");
                            continue;
                        }

                        if (original == null || !_manifest.ContainsKey(original.name))
                        {
                            continue;
                        }

                        _activeSwapKeys.Add(swapKey);
                        names.Add(original.name);
                        pendingSwaps.Add(new PendingSwap
                        {
                            Renderer = renderer,
                            MatIndex = i,
                            Prop = prop,
                            Original = original,
                            Name = original.name,
                        });
                    }
                }
            }
        }

        EnqueuePendingSwaps(pendingSwaps, names);
        return pendingSwaps.Count;
    }

    private void EnqueuePendingSwaps(List<PendingSwap> pendingSwaps, HashSet<string> names)
    {
        if (pendingSwaps.Count == 0)
        {
            return;
        }

        foreach (var swap in pendingSwaps)
        {
            List<PendingSwap> swaps;
            if (!_pendingSwapsByName.TryGetValue(swap.Name, out swaps))
            {
                swaps = new List<PendingSwap>();
                _pendingSwapsByName[swap.Name] = swaps;
            }

            swaps.Add(swap);
        }

        foreach (var name in names)
        {
            EnsureRawLoadStarted(name);
        }

        if (_applyRoutine == null)
        {
            _applyRoutine = StartCoroutine(ProcessPendingSwaps(_generation));
        }
    }

    private void EnsureRawLoadStarted(string name)
    {
        Texture2D cached;
        if (TryGetCachedTexture(name, out cached))
        {
            return;
        }

        if (_loadTasks.ContainsKey(name))
        {
            return;
        }

        Entry entry;
        if (!_manifest.TryGetValue(name, out entry))
        {
            Logger.LogError($"하이레즈 매니페스트 항목 없음: {name}");
            return;
        }

        try
        {
            var path = Path.Combine(_hiresDir, entry.File);
            Logger.LogInfo($"하이레즈 로드 시작: {entry.Name}");
            _loadTasks[name] = Task.Run(() => LoadRawInBackground(name, entry, path));
        }
        catch (Exception e)
        {
            Logger.LogError($"하이레즈 로드 작업 시작 실패: {name}\n{e}");
        }
    }

    private static LoadResult LoadRawInBackground(string name, Entry entry, string path)
    {
        var stopwatch = Stopwatch.StartNew();
        var raw = ReadGzipRaw(path, entry.RawSize);
        stopwatch.Stop();

        return new LoadResult
        {
            Name = name,
            Entry = entry,
            Raw = raw,
            ElapsedMs = stopwatch.ElapsedMilliseconds,
        };
    }

    private IEnumerator ProcessPendingSwaps(int generation)
    {
        while (_pendingSwapsByName.Count > 0)
        {
            if (generation != _generation)
            {
                _applyRoutine = null;
                yield break;
            }

            string name;
            Texture2D cached;
            if (TryGetCachedReadyName(out name, out cached))
            {
                ApplyPendingSwaps(name, cached, generation);
                PruneCacheAndUnload();
                yield return null;
                continue;
            }

            Task<LoadResult> task;
            if (TryGetCompletedLoad(out name, out task))
            {
                ProcessCompletedLoad(name, task, generation);
                yield return null;
                continue;
            }

            if (DropMissingPendingLoad())
            {
                yield return null;
                continue;
            }

            yield return null;
        }

        _applyRoutine = null;
    }

    private bool TryGetCachedReadyName(out string name, out Texture2D texture)
    {
        foreach (var pendingName in _pendingSwapsByName.Keys.ToArray())
        {
            if (TryGetCachedTexture(pendingName, out texture))
            {
                name = pendingName;
                return true;
            }
        }

        name = null;
        texture = null;
        return false;
    }

    private bool TryGetCompletedLoad(out string name, out Task<LoadResult> task)
    {
        foreach (var pendingName in _pendingSwapsByName.Keys.ToArray())
        {
            if (_loadTasks.TryGetValue(pendingName, out task) && task.IsCompleted)
            {
                name = pendingName;
                return true;
            }
        }

        name = null;
        task = null;
        return false;
    }

    private bool DropMissingPendingLoad()
    {
        foreach (var name in _pendingSwapsByName.Keys.ToArray())
        {
            if (!_cache.ContainsKey(name) && !_loadTasks.ContainsKey(name))
            {
                _pendingSwapsByName.Remove(name);
                Logger.LogError($"하이레즈 로드 작업 없음: {name}");
                return true;
            }
        }

        return false;
    }

    private void ProcessCompletedLoad(string name, Task<LoadResult> task, int generation)
    {
        if (task.IsCanceled)
        {
            _loadTasks.Remove(name);
            _pendingSwapsByName.Remove(name);
            Logger.LogError($"하이레즈 로드 취소: {name}");
            return;
        }

        if (task.IsFaulted)
        {
            _loadTasks.Remove(name);
            _pendingSwapsByName.Remove(name);
            Logger.LogError($"하이레즈 로드 실패: {name}\n{task.Exception}");
            return;
        }

        Texture2D texture = null;
        try
        {
            var result = task.Result;
            var entry = result.Entry;

            var uploadStopwatch = Stopwatch.StartNew();
            texture = new Texture2D(entry.Width, entry.Height, (TextureFormat)entry.Format, entry.MipCount, entry.Linear);
            texture.LoadRawTextureData(result.Raw);
            texture.Apply(false, true);
            texture.name = entry.Name + "@hires";
            uploadStopwatch.Stop();

            AddToCache(result.Name, texture, entry.RawSize);
            _loadTasks.Remove(name);
            Logger.LogInfo($"하이레즈 로드 완료: {entry.Name} ({result.ElapsedMs}ms, {result.Raw.Length / 1024f / 1024f:0.0}MB)");
            Logger.LogDebug($"하이레즈 업로드 1장 완료: {entry.Name} ({uploadStopwatch.ElapsedMilliseconds}ms)");

            ApplyPendingSwaps(result.Name, texture, generation);
            PruneCacheAndUnload();
        }
        catch (Exception e)
        {
            _loadTasks.Remove(name);
            _pendingSwapsByName.Remove(name);
            Logger.LogError($"하이레즈 업로드 실패: {name}\n{e}");
            if (texture != null)
            {
                UnityEngine.Object.Destroy(texture);
                Resources.UnloadUnusedAssets();
            }
        }
    }

    private void ApplyPendingSwaps(string name, Texture2D texture, int generation)
    {
        if (generation != _generation)
        {
            return;
        }

        List<PendingSwap> pendingSwaps;
        if (!_pendingSwapsByName.TryGetValue(name, out pendingSwaps))
        {
            return;
        }

        var applied = 0;
        foreach (var swap in pendingSwaps)
        {
            if (generation != _generation)
            {
                return;
            }

            try
            {
                if (swap.Renderer == null)
                {
                    continue;
                }

                var liveMaterials = swap.Renderer.materials;
                if (liveMaterials == null || swap.MatIndex < 0 || swap.MatIndex >= liveMaterials.Length || liveMaterials[swap.MatIndex] == null)
                {
                    continue;
                }

                liveMaterials[swap.MatIndex].SetTexture(swap.Prop, texture);
                _swaps.Add(new Swap
                {
                    Renderer = swap.Renderer,
                    MatIndex = swap.MatIndex,
                    Prop = swap.Prop,
                    Original = swap.Original,
                    HiresName = name,
                });
                TouchCache(name);
                applied++;
            }
            catch (Exception e)
            {
                Logger.LogError($"하이레즈 텍스처 적용 실패: {name}/{swap.Prop}\n{e}");
            }
        }

        _pendingSwapsByName.Remove(name);
        Logger.LogInfo($"하이레즈 스왑 적용: {name} {applied}/{pendingSwaps.Count}개");
    }

    private static byte[] ReadGzipRaw(string path, long rawSize)
    {
        if (rawSize < 0 || rawSize > int.MaxValue)
        {
            throw new InvalidDataException($"rawSize 범위 오류: {rawSize}");
        }

        var compressed = File.ReadAllBytes(path);
        var raw = new byte[(int)rawSize];
        using (var input = new MemoryStream(compressed))
        using (var gzip = new GZipStream(input, CompressionMode.Decompress))
        {
            var offset = 0;
            while (offset < raw.Length)
            {
                var read = gzip.Read(raw, offset, raw.Length - offset);
                if (read == 0)
                {
                    break;
                }

                offset += read;
            }

            if (offset != raw.Length)
            {
                throw new InvalidDataException($"gzip 해제 크기 부족: {offset} != {raw.Length}");
            }

            if (gzip.ReadByte() != -1)
            {
                throw new InvalidDataException("gzip 해제 크기가 manifest rawSize보다 큽니다.");
            }
        }

        return raw;
    }

    private void Restore()
    {
        var swapCount = _swaps.Count;
        var pendingCount = CountPendingSwaps();
        var reservedCount = _activeSwapKeys.Count;
        _generation++;

        if (_applyRoutine != null)
        {
            StopCoroutine(_applyRoutine);
            _applyRoutine = null;
        }

        for (var i = _swaps.Count - 1; i >= 0; i--)
        {
            var swap = _swaps[i];
            try
            {
                if (swap.Renderer == null)
                {
                    continue;
                }

                var liveMaterials = swap.Renderer.materials;
                if (liveMaterials == null || swap.MatIndex < 0 || swap.MatIndex >= liveMaterials.Length || liveMaterials[swap.MatIndex] == null)
                {
                    continue;
                }

                liveMaterials[swap.MatIndex].SetTexture(swap.Prop, swap.Original);
            }
            catch (Exception e)
            {
                Logger.LogError($"하이레즈 원복 실패: {swap.Prop}\n{e}");
            }
        }

        _pendingSwapsByName.Clear();
        _swaps.Clear();
        _activeSwapKeys.Clear();

        var destroyed = GetCacheBudgetBytes() == 0
            ? DestroyAllCachedTextures()
            : PruneCache();

        if (destroyed > 0)
        {
            Resources.UnloadUnusedAssets();
        }

        if (swapCount > 0 || pendingCount > 0 || reservedCount > 0 || destroyed > 0)
        {
            Logger.LogInfo($"하이레즈 원복 완료: 스왑 {swapCount}개, 대기 {pendingCount}개, 텍스처 {destroyed}개 해제");
        }
    }

    private bool TryGetCachedTexture(string name, out Texture2D texture)
    {
        CacheItem item;
        if (!_cache.TryGetValue(name, out item))
        {
            texture = null;
            return false;
        }

        if (item.Texture == null)
        {
            _cache.Remove(name);
            _cacheBytes -= item.Bytes;
            if (_cacheBytes < 0)
            {
                _cacheBytes = 0;
            }

            texture = null;
            return false;
        }

        TouchCache(name, item);
        texture = item.Texture;
        Logger.LogDebug($"하이레즈 캐시 히트: {name}");
        return true;
    }

    private void AddToCache(string name, Texture2D texture, long bytes)
    {
        CacheItem oldItem;
        if (_cache.TryGetValue(name, out oldItem))
        {
            _cacheBytes -= oldItem.Bytes;
            if (oldItem.Texture != null && oldItem.Texture != texture)
            {
                UnityEngine.Object.Destroy(oldItem.Texture);
                Resources.UnloadUnusedAssets();
            }
        }

        if (bytes < 0)
        {
            bytes = 0;
        }

        _cache[name] = new CacheItem
        {
            Texture = texture,
            Bytes = bytes,
            LastUsed = ++_lruCounter,
        };
        _cacheBytes += bytes;
    }

    private void TouchCache(string name)
    {
        CacheItem item;
        if (_cache.TryGetValue(name, out item))
        {
            TouchCache(name, item);
        }
    }

    private void TouchCache(string name, CacheItem item)
    {
        item.LastUsed = ++_lruCounter;
        _cache[name] = item;
    }

    private void PruneCacheAndUnload()
    {
        var destroyed = PruneCache();
        if (destroyed > 0)
        {
            Resources.UnloadUnusedAssets();
        }
    }

    private int PruneCache()
    {
        var budgetBytes = GetCacheBudgetBytes();
        if (budgetBytes == 0)
        {
            return 0;
        }

        var destroyed = 0;
        while (_cacheBytes > budgetBytes)
        {
            var oldestName = FindOldestEvictableCacheName();
            if (oldestName == null)
            {
                break;
            }

            if (DestroyCachedTexture(oldestName, true))
            {
                destroyed++;
            }
        }

        return destroyed;
    }

    private string FindOldestEvictableCacheName()
    {
        string oldestName = null;
        var oldestUsed = long.MaxValue;
        foreach (var pair in _cache)
        {
            if (IsCacheNameInUse(pair.Key))
            {
                continue;
            }

            if (pair.Value.LastUsed < oldestUsed)
            {
                oldestName = pair.Key;
                oldestUsed = pair.Value.LastUsed;
            }
        }

        return oldestName;
    }

    private bool IsCacheNameInUse(string name)
    {
        foreach (var swap in _swaps)
        {
            if (string.Equals(swap.HiresName, name, StringComparison.Ordinal))
            {
                return true;
            }
        }

        return false;
    }

    private bool DestroyCachedTexture(string name, bool logEviction)
    {
        CacheItem item;
        if (!_cache.TryGetValue(name, out item))
        {
            return false;
        }

        _cache.Remove(name);
        _cacheBytes -= item.Bytes;
        if (_cacheBytes < 0)
        {
            _cacheBytes = 0;
        }

        if (item.Texture == null)
        {
            return false;
        }

        UnityEngine.Object.Destroy(item.Texture);
        if (logEviction)
        {
            Logger.LogInfo($"하이레즈 캐시 축출: {name} ({item.Bytes / 1024f / 1024f:0.0}MB)");
        }

        return true;
    }

    private int DestroyAllCachedTextures()
    {
        var names = _cache.Keys.ToArray();
        var destroyed = 0;
        foreach (var name in names)
        {
            if (DestroyCachedTexture(name, false))
            {
                destroyed++;
            }
        }

        _cacheBytes = 0;
        return destroyed;
    }

    private long GetCacheBudgetBytes()
    {
        var mb = _cacheBudgetMb != null ? _cacheBudgetMb.Value : 256;
        if (mb <= 0)
        {
            return 0;
        }

        return mb * 1024L * 1024L;
    }

    private int CountPendingSwaps()
    {
        var count = 0;
        foreach (var swaps in _pendingSwapsByName.Values)
        {
            count += swaps.Count;
        }

        return count;
    }

    private static string MakeSwapKey(Renderer renderer, int matIndex, string prop)
    {
        return renderer.GetInstanceID() + ":" + matIndex + ":" + prop;
    }

    private static string FormatMethod(MethodInfo method)
    {
        var args = string.Join(", ", method.GetParameters().Select(p => p.ParameterType.Name + " " + p.Name).ToArray());
        return method.DeclaringType.FullName + "." + method.Name + "(" + args + ")";
    }

    private void DumpMethods(Type targetType)
    {
        try
        {
            var methods = targetType
                .GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                .Select(FormatMethod)
                .Distinct()
                .OrderBy(x => x)
                .ToArray();
            Logger.LogInfo("ItemSpecificationPanel 메서드 목록:\n" + string.Join("\n", methods));
        }
        catch (Exception e)
        {
            Logger.LogError($"메서드 목록 출력 실패: {e}");
        }
    }

    private void DumpSpecificationTypes()
    {
        foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
        {
            Type[] types;
            try
            {
                types = assembly.GetTypes();
            }
            catch (ReflectionTypeLoadException e)
            {
                types = e.Types.Where(t => t != null).ToArray();
            }
            catch (Exception e)
            {
                Logger.LogWarning($"어셈블리 타입 읽기 실패: {assembly.FullName}\n{e}");
                continue;
            }

            foreach (var type in types)
            {
                if (type.FullName != null && type.FullName.IndexOf("Specification", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    Logger.LogInfo($"Specification 후보 타입: {type.FullName}");
                }
            }
        }
    }

    private sealed class Entry
    {
        public string Name { get; set; }
        public string File { get; set; }
        public int Width { get; set; }
        public int Height { get; set; }
        public int MipCount { get; set; }
        public int Format { get; set; }
        public bool Linear { get; set; }
        public long RawSize { get; set; }
    }

    private sealed class LoadResult
    {
        public string Name;
        public Entry Entry;
        public byte[] Raw;
        public long ElapsedMs;
    }

    private sealed class CacheItem
    {
        public Texture2D Texture;
        public long Bytes;
        public long LastUsed;
    }

    private sealed class PendingSwap
    {
        public Renderer Renderer;
        public int MatIndex;
        public string Prop;
        public Texture Original;
        public string Name;
    }

    private sealed class Swap
    {
        public Renderer Renderer;
        public int MatIndex;
        public string Prop;
        public Texture Original;
        public string HiresName;
    }
}
