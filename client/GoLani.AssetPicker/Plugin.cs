using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using BepInEx;
using BepInEx.Configuration;
using Newtonsoft.Json;
using UnityEngine;

[BepInPlugin("com.golani.assetpicker", "GoLani Asset Picker", "1.0.0")]
public class Plugin : BaseUnityPlugin
{
    private readonly Dictionary<string, List<string>> _texIndex = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
    private readonly List<string> _lastResult = new List<string>();

    private ConfigEntry<KeyCode> _toggleKey;
    private ConfigEntry<string> _pickButton;
    private bool _pickMode;
    private GUIStyle _bannerStyle;
    private GUIStyle _resultStyle;

    private void Awake()
    {
        try
        {
            _toggleKey = Config.Bind(
                "General",
                "ToggleKey",
                KeyCode.F8,
                "에셋 피커 모드를 켜고 끄는 키");

            _pickButton = Config.Bind(
                "General",
                "PickButton",
                "마우스 왼쪽 버튼",
                "피커 모드에서 에셋을 선택하는 버튼 설명. 실제 입력은 마우스 왼쪽 버튼입니다.");

            LoadTextureIndex();
        }
        catch (Exception e)
        {
            Logger.LogError($"에셋 피커 초기화 실패: {e}");
        }
    }

    private void Update()
    {
        try
        {
            if (_toggleKey != null && Input.GetKeyDown(_toggleKey.Value))
            {
                _pickMode = !_pickMode;
                Logger.LogInfo(_pickMode ? "에셋 피커 모드 켜짐" : "에셋 피커 모드 꺼짐");
            }

            if (_pickMode && Input.GetMouseButtonDown(0))
            {
                Pick();
            }
        }
        catch (Exception e)
        {
            Logger.LogError($"에셋 피커 업데이트 실패: {e}");
        }
    }

    private void OnGUI()
    {
        try
        {
            if (!_pickMode)
            {
                return;
            }

            EnsureStyles();
            var keyName = _toggleKey != null ? _toggleKey.Value.ToString() : "F8";
            var oldColor = GUI.color;

            GUI.color = new Color(0f, 0f, 0f, 0.72f);
            GUI.Box(new Rect(12f, 12f, 330f, 34f), GUIContent.none);
            GUI.color = oldColor;
            GUI.Label(new Rect(22f, 19f, 310f, 22f), $"PICK MODE ({keyName}로 해제) - 에셋 클릭", _bannerStyle);

            if (_lastResult.Count == 0)
            {
                return;
            }

            var text = string.Join("\n", _lastResult.ToArray());
            var width = Mathf.Min(Screen.width - 24f, 780f);
            var height = Mathf.Min(Screen.height - 64f, 420f);
            GUI.color = new Color(0f, 0f, 0f, 0.72f);
            GUI.Box(new Rect(12f, 54f, width, height), GUIContent.none);
            GUI.color = oldColor;
            GUI.Label(new Rect(22f, 64f, width - 20f, height - 20f), text, _resultStyle);
        }
        catch (Exception e)
        {
            Logger.LogError($"에셋 피커 화면 표시 실패: {e}");
        }
    }

    private void LoadTextureIndex()
    {
        var pluginDir = Path.GetDirectoryName(Info.Location);
        if (string.IsNullOrEmpty(pluginDir))
        {
            Logger.LogWarning("플러그인 위치를 찾지 못해 인덱스를 로드하지 못했습니다.");
            return;
        }

        var path = Path.Combine(pluginDir, "texindex.json");
        if (!File.Exists(path))
        {
            Logger.LogWarning($"텍스처 인덱스 없음: {path}");
            return;
        }

        var json = File.ReadAllText(path);
        var loaded = JsonConvert.DeserializeObject<Dictionary<string, List<string>>>(json);
        if (loaded == null || loaded.Count == 0)
        {
            Logger.LogWarning("텍스처 인덱스가 비어 있습니다.");
            return;
        }

        foreach (var pair in loaded)
        {
            if (string.IsNullOrEmpty(pair.Key) || pair.Value == null)
            {
                continue;
            }

            _texIndex[pair.Key] = pair.Value.Where(k => !string.IsNullOrEmpty(k)).Distinct().ToList();
        }

        Logger.LogInfo($"텍스처 인덱스 {_texIndex.Count}개 로드");
    }

    private void Pick()
    {
        try
        {
            _lastResult.Clear();

            var cam = GetPickCamera();
            if (cam == null)
            {
                SetResult("카메라를 찾지 못했습니다.");
                Logger.LogWarning("에셋 피커: 카메라 없음");
                return;
            }

            var ray = cam.ScreenPointToRay(Input.mousePosition);
            if (!Physics.Raycast(ray, out var hit, 1000f))
            {
                SetResult("레이캐스트 미스(콜라이더 없음)");
                Logger.LogInfo("레이캐스트 미스(콜라이더 없음)");
                return;
            }

            var renderer = FindRenderer(hit.collider);
            if (renderer == null)
            {
                SetResult($"렌더러 없음: {GetPath(hit.collider.transform)}");
                Logger.LogInfo(string.Join("\n", _lastResult.ToArray()));
                return;
            }

            BuildResult(hit, renderer);
            Logger.LogInfo(string.Join("\n", _lastResult.ToArray()));
        }
        catch (Exception e)
        {
            Logger.LogError($"에셋 픽 실패: {e}");
        }
    }

    private Camera GetPickCamera()
    {
        if (Camera.main != null && Camera.main.enabled)
        {
            return Camera.main;
        }

        Camera best = null;
        foreach (var cam in Camera.allCameras)
        {
            if (cam == null || !cam.enabled)
            {
                continue;
            }

            if (best == null || cam.depth > best.depth)
            {
                best = cam;
            }
        }

        return best;
    }

    private static Renderer FindRenderer(Collider collider)
    {
        if (collider == null)
        {
            return null;
        }

        var renderer = collider.GetComponent<Renderer>();
        if (renderer != null)
        {
            return renderer;
        }

        renderer = collider.GetComponentInParent<Renderer>();
        if (renderer != null)
        {
            return renderer;
        }

        return collider.GetComponentInChildren<Renderer>();
    }

    private void BuildResult(RaycastHit hit, Renderer renderer)
    {
        var hitPath = GetPath(hit.collider.transform);
        var rendererPath = GetPath(renderer.transform);
        var meshName = GetMeshName(renderer);

        _lastResult.Add($"GO경로: {hitPath}");
        if (!string.Equals(hitPath, rendererPath, StringComparison.Ordinal))
        {
            _lastResult.Add($"렌더러: {rendererPath}");
        }

        _lastResult.Add($"메시: {meshName}");
        _lastResult.Add($"콜라이더: {hit.collider.GetType().Name} / {hit.collider.name}");
        _lastResult.Add($"선택버튼: {(_pickButton != null ? _pickButton.Value : "마우스 왼쪽 버튼")}");

        var materials = renderer.sharedMaterials;
        if (materials == null || materials.Length == 0)
        {
            _lastResult.Add("머티리얼: 없음");
            return;
        }

        for (var i = 0; i < materials.Length; i++)
        {
            var mat = materials[i];
            if (mat == null)
            {
                _lastResult.Add($"머티리얼[{i}]: 없음");
                continue;
            }

            _lastResult.Add($"머티리얼[{i}]: {mat.name}");
            _lastResult.Add($"  셰이더: {(mat.shader != null ? mat.shader.name : "없음")}");

            string[] props;
            try
            {
                props = mat.GetTexturePropertyNames();
            }
            catch (Exception e)
            {
                Logger.LogError($"텍스처 속성 읽기 실패: {mat.name}: {e}");
                _lastResult.Add("  텍스처속성: 읽기 실패");
                continue;
            }

            if (props == null || props.Length == 0)
            {
                _lastResult.Add("  텍스처속성: 없음");
                continue;
            }

            foreach (var prop in props)
            {
                Texture tex;
                try
                {
                    tex = mat.GetTexture(prop);
                }
                catch (Exception e)
                {
                    Logger.LogError($"텍스처 읽기 실패: {mat.name}/{prop}: {e}");
                    _lastResult.Add($"  {prop}: 읽기 실패");
                    continue;
                }

                if (tex == null)
                {
                    _lastResult.Add($"  {prop}: 없음");
                    continue;
                }

                AppendTexture(prop, tex);
            }
        }
    }

    private void AppendTexture(string prop, Texture tex)
    {
        var line = new StringBuilder();
        line.Append("  ");
        line.Append(prop);
        line.Append(": ");
        line.Append(tex.name);

        if (tex is Texture2D tex2d)
        {
            line.Append($" ({tex2d.width}x{tex2d.height}, {tex2d.format}, 밉 {tex2d.mipmapCount})");
        }
        else
        {
            line.Append($" ({tex.GetType().Name}, {tex.width}x{tex.height})");
        }

        _lastResult.Add(line.ToString());

        if (!string.IsNullOrEmpty(tex.name) && _texIndex.TryGetValue(tex.name, out var keys) && keys.Count > 0)
        {
            foreach (var key in keys)
            {
                _lastResult.Add($"    번들 key: {key}");
            }
        }
        else
        {
            _lastResult.Add("    번들 key: (인덱스에 없음)");
        }
    }

    private void SetResult(string message)
    {
        _lastResult.Clear();
        _lastResult.Add(message);
    }

    private static string GetPath(Transform transform)
    {
        if (transform == null)
        {
            return "(없음)";
        }

        var parts = new List<string>();
        var current = transform;
        while (current != null)
        {
            parts.Add(current.name);
            current = current.parent;
        }

        parts.Reverse();
        return string.Join("/", parts.ToArray());
    }

    private static string GetMeshName(Renderer renderer)
    {
        if (renderer == null)
        {
            return "(없음)";
        }

        var meshFilter = renderer.GetComponent<MeshFilter>();
        if (meshFilter != null && meshFilter.sharedMesh != null)
        {
            return meshFilter.sharedMesh.name;
        }

        var skinned = renderer as SkinnedMeshRenderer;
        if (skinned != null && skinned.sharedMesh != null)
        {
            return skinned.sharedMesh.name;
        }

        return "(없음)";
    }

    private void EnsureStyles()
    {
        if (_bannerStyle == null)
        {
            _bannerStyle = new GUIStyle(GUI.skin.label)
            {
                fontSize = 15,
                fontStyle = FontStyle.Bold,
                normal = { textColor = Color.white }
            };
        }

        if (_resultStyle == null)
        {
            _resultStyle = new GUIStyle(GUI.skin.label)
            {
                fontSize = 13,
                wordWrap = false,
                normal = { textColor = Color.white }
            };
        }
    }
}
