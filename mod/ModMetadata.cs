using System.Collections.Generic;
using SPTarkov.Server.Core.Models.Spt.Mod;
using Range = SemanticVersioning.Range;
using Version = SemanticVersioning.Version;

namespace GoLani.ItemTextureKoreanChange;

// SPT 4.1 번들 모드 메타데이터. 서버가 모드 폴더의 bundles.json + bundles/ 를 로드함.
// 별도 로직 클래스는 불필요하며 텍스처 교체는 번들 로더가 처리함.
public record ModMetadata : IModMetadata
{
    public string ModGuid { get; init; } = "com.golani.itemtexturekoreanchange";
    public string Name { get; init; } = "GoLani.ItemTextureKoreanChange";
    public string Author { get; init; } = "Golani";
    public List<string>? Contributors { get; init; }
    public Version Version { get; init; } = new("1.1.0");
    public Range SptVersion { get; init; } = new("~4.1.0");
    public bool HasPrepatcher { get; init; } = false;
    public List<string>? Incompatibilities { get; init; }
    public Dictionary<string, Range>? ModDependencies { get; init; }
    public string? Url { get; init; }
    public string License { get; init; } = "MIT";
}
