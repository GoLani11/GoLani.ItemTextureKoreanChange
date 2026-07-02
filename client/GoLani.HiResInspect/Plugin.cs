using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using BepInEx;
using HarmonyLib;
using Newtonsoft.Json;
using UnityEngine;

[BepInPlugin("com.golani.hiresinspect", "GoLani HiRes Inspect", "1.0.0")]
public class Plugin : BaseUnityPlugin
{
    private static Plugin _instance;

    private readonly Dictionary<string, Entry> _manifest = new Dictionary<string, Entry>(StringComparer.Ordinal);
    private readonly Dictionary<string, Texture2D> _cache = new Dictionary<string, Texture2D>(StringComparer.Ordinal);
    private readonly List<Swap> _swaps = new List<Swap>();
    private readonly HashSet<string> _activeSwapKeys = new HashSet<string>(StringComparer.Ordinal);

    private Harmony _harmony;
    private string _hiresDir;
    private Coroutine _scanRoutine;

    private void Awake()
    {
        _instance = this;

        try
        {
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

            Restore();
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

    private static void OnInspectShown()
    {
        try
        {
            if (_instance != null)
            {
                _instance.HandleInspectShown();
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

    private void HandleInspectShown()
    {
        try
        {
            if (_scanRoutine != null)
            {
                StopCoroutine(_scanRoutine);
                _scanRoutine = null;
            }

            if (_swaps.Count > 0 || _cache.Count > 0)
            {
                Restore();
            }

            _scanRoutine = StartCoroutine(ScanAfterInspectShown());
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

    private IEnumerator ScanAfterInspectShown()
    {
        yield return null;
        ScanRenderers();
        yield return new WaitForSeconds(0.5f);
        ScanRenderers();
        _scanRoutine = null;
    }

    private void ScanRenderers()
    {
        try
        {
            var applied = 0;
            foreach (var renderer in UnityEngine.Object.FindObjectsOfType<Renderer>())
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

                        var hires = GetOrLoad(original.name);
                        if (hires == null)
                        {
                            continue;
                        }

                        try
                        {
                            var liveMaterials = renderer.materials;
                            if (liveMaterials == null || i >= liveMaterials.Length || liveMaterials[i] == null)
                            {
                                continue;
                            }

                            liveMaterials[i].SetTexture(prop, hires);
                            _swaps.Add(new Swap
                            {
                                Renderer = renderer,
                                MatIndex = i,
                                Prop = prop,
                                Original = original,
                            });
                            _activeSwapKeys.Add(swapKey);
                            applied++;
                        }
                        catch (Exception e)
                        {
                            Logger.LogError($"하이레즈 텍스처 적용 실패: {original.name}/{prop}\n{e}");
                        }
                    }
                }
            }

            Logger.LogInfo($"하이레즈 스캔 완료: 스왑 {applied}개");
        }
        catch (Exception e)
        {
            Logger.LogError($"하이레즈 스캔 실패: {e}");
        }
    }

    private Texture2D GetOrLoad(string name)
    {
        Texture2D cached;
        if (_cache.TryGetValue(name, out cached))
        {
            return cached;
        }

        Texture2D texture = null;
        try
        {
            var entry = _manifest[name];
            var path = Path.Combine(_hiresDir, entry.File);
            // inspect를 여는 순간만 동기 로드한다. 느려지면 이후 비동기로 바꿀 수 있다.
            var raw = ReadGzipRaw(path, entry.RawSize);
            texture = new Texture2D(entry.Width, entry.Height, (TextureFormat)entry.Format, entry.MipCount, entry.Linear);
            texture.LoadRawTextureData(raw);
            texture.Apply(false, true);
            texture.name = entry.Name + "@hires";
            _cache[name] = texture;
            Logger.LogInfo($"하이레즈 로드: {entry.Name} ({raw.Length / 1024f / 1024f:0.0}MB)");
            return texture;
        }
        catch (Exception e)
        {
            Logger.LogError($"하이레즈 로드 실패: {name}\n{e}");
            if (texture != null)
            {
                UnityEngine.Object.Destroy(texture);
            }

            return null;
        }
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
        var textureCount = _cache.Count;
        if (swapCount == 0 && textureCount == 0)
        {
            return; // 스왑 없었으면 UnloadUnusedAssets 히치도 피한다
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

        foreach (var texture in _cache.Values)
        {
            if (texture != null)
            {
                UnityEngine.Object.Destroy(texture);
            }
        }

        _cache.Clear();
        _swaps.Clear();
        _activeSwapKeys.Clear();
        Resources.UnloadUnusedAssets();
        Logger.LogInfo($"하이레즈 원복 완료: 스왑 {swapCount}개, 텍스처 {textureCount}개 해제");
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

    private sealed class Swap
    {
        public Renderer Renderer;
        public int MatIndex;
        public string Prop;
        public Texture Original;
    }
}
