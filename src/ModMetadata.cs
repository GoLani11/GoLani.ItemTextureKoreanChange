using SPTarkov.Server.Core.Models.Spt.Mod;
using Range = SemanticVersioning.Range;
using Version = SemanticVersioning.Version;

namespace GoLani.ItemTextureKoreanChange;

// SPT 4.x 번들 모드 메타데이터. IsBundleMod=true 면 서버가 bundles.json + bundles/ 를 로드함.
// 별도 로직 클래스 불필요(텍스처 교체는 번들 로더가 처리).
public record ModMetadata : AbstractModMetadata
{
    public override string ModGuid { get; init; } = "com.golani.itemtexturekoreanchange";
    public override string Name { get; init; } = "GoLani.ItemTextureKoreanChange";
    public override string Author { get; init; } = "Golani";
    public override List<string>? Contributors { get; init; }
    public override Version Version { get; init; } = new("1.1.0");
    public override Range SptVersion { get; init; } = new("~4.0.0");
    public override List<string>? Incompatibilities { get; init; }
    public override Dictionary<string, Range>? ModDependencies { get; init; }
    public override string? Url { get; init; }
    public override bool? IsBundleMod { get; init; } = true;
    public override string License { get; init; } = "MIT";
}
