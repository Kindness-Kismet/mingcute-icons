#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SVG_ROOT = ROOT / "svg"


def pascal_icon_name(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)


def icon_sort_key(svg_path: Path) -> tuple[str, str]:
    stem = svg_path.stem
    base = stem.removesuffix("_fill").removesuffix("_line")
    variant = "0" if stem.endswith("_fill") else "1"
    return base, variant


def read_svg_layers(svg_path: Path) -> list[tuple[str, float]]:
    try:
        tree = ET.parse(svg_path)
    except ET.ParseError as error:
        raise RuntimeError(f"无法解析 SVG: {svg_path}: {error}") from error

    layers: list[tuple[str, float]] = []
    for element in tree.iter():
        if element.tag.rsplit("}", 1)[-1] != "path":
            continue

        data = (element.attrib.get("d") or "").strip()
        if not data:
            continue

        fill = (element.attrib.get("fill") or "").strip().lower()
        if not fill or fill == "none":
            continue

        compact = re.sub(r"\s+", "", data).lower()
        if compact.startswith("m240v24h0v0z"):
            continue

        opacity_text = element.attrib.get("opacity") or element.attrib.get("fill-opacity")
        opacity = 1.0
        if opacity_text is not None:
            try:
                opacity = float(opacity_text)
            except ValueError:
                opacity = 1.0

        if opacity <= 0:
            continue

        layers.append((data, min(opacity, 1.0)))

    if not layers:
        raise RuntimeError(f"未找到可用路径: {svg_path}")

    return layers


def extract_paths(svg_path: Path) -> str:
    return " ".join(data for data, _ in read_svg_layers(svg_path))


def extract_icon_layers(svg_path: Path) -> list[dict[str, object]]:
    return [{"data": data, "opacity": opacity} for data, opacity in read_svg_layers(svg_path)]


def patch_text(path: Path, replacements: list[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8-sig")
    for old, new in replacements:
        if old not in text:
            raise RuntimeError(f"补丁锚点缺失: {path}: {old[:80]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def patch_layer_support(iconpacks_dir: Path) -> None:
    core_dir = iconpacks_dir / "src" / "IconPacks.Avalonia.Core"
    mingcute_dir = iconpacks_dir / "src" / "IconPacks.Avalonia.MingCuteIcons"

    (core_dir / "PackIconLayer.cs").write_text(
        """using Avalonia.Media;

namespace IconPacks.Avalonia.Core
{
    public sealed class PackIconLayer
    {
        public PackIconLayer(StreamGeometry data, double opacity)
        {
            this.Data = data;
            this.Opacity = opacity;
        }

        public StreamGeometry Data { get; }

        public double Opacity { get; }
    }
}
""",
        encoding="utf-8")

    (core_dir / "PackIconLayerView.cs").write_text(
        """using System;
using System.Collections.Generic;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;

namespace IconPacks.Avalonia.Core
{
    public sealed class PackIconLayerView : Control
    {
        public static readonly StyledProperty<IReadOnlyList<PackIconLayer>> LayersProperty
            = AvaloniaProperty.Register<PackIconLayerView, IReadOnlyList<PackIconLayer>>(nameof(Layers));

        public static readonly StyledProperty<IBrush> ForegroundProperty
            = AvaloniaProperty.Register<PackIconLayerView, IBrush>(nameof(Foreground), Brushes.Black);

        static PackIconLayerView()
        {
            AffectsRender<PackIconLayerView>(LayersProperty, ForegroundProperty);
        }

        public IReadOnlyList<PackIconLayer> Layers
        {
            get { return this.GetValue(LayersProperty); }
            set { this.SetValue(LayersProperty, value); }
        }

        public IBrush Foreground
        {
            get { return this.GetValue(ForegroundProperty); }
            set { this.SetValue(ForegroundProperty, value); }
        }

        protected override Size MeasureOverride(Size availableSize)
        {
            var bounds = GetLayerBounds(this.Layers);
            return bounds.HasValue
                ? new Size(bounds.Value.Width, bounds.Value.Height)
                : default;
        }

        public override void Render(DrawingContext context)
        {
            base.Render(context);
            var layers = this.Layers;
            var bounds = GetLayerBounds(layers);
            if (layers is null || layers.Count == 0 || !bounds.HasValue || Bounds.Width <= 0 || Bounds.Height <= 0)
            {
                return;
            }

            var iconBounds = bounds.Value;
            var scale = Math.Min(Bounds.Width / iconBounds.Width, Bounds.Height / iconBounds.Height);
            if (double.IsNaN(scale) || double.IsInfinity(scale) || scale <= 0)
            {
                return;
            }

            var left = (Bounds.Width - iconBounds.Width * scale) / 2;
            var top = (Bounds.Height - iconBounds.Height * scale) / 2;
            var matrix =
                Matrix.CreateTranslation(-iconBounds.X, -iconBounds.Y)
                * Matrix.CreateScale(scale, scale)
                * Matrix.CreateTranslation(left, top);

            using (context.PushTransform(matrix))
            {
                foreach (var layer in layers)
                {
                    using (context.PushOpacity(layer.Opacity))
                    {
                        context.DrawGeometry(this.Foreground, null, layer.Data);
                    }
                }
            }
        }

        private static Rect? GetLayerBounds(IReadOnlyList<PackIconLayer> layers)
        {
            if (layers is null || layers.Count == 0)
            {
                return null;
            }

            var bounds = layers[0].Data.Bounds;
            for (var i = 1; i < layers.Count; i++)
            {
                bounds = bounds.Union(layers[i].Data.Bounds);
            }

            return bounds.Width > 0 && bounds.Height > 0 ? bounds : null;
        }
    }
}
""",
        encoding="utf-8")

    (core_dir / "PackIconLayerDataFactory.cs").write_text(
        """using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Text.Json;
using Avalonia.Media;
using Avalonia.Platform;

namespace IconPacks.Avalonia.Core
{
    public static class PackIconLayerDataFactory<TEnum> where TEnum : struct, Enum
    {
        public static Lazy<ReadOnlyDictionary<TEnum, IReadOnlyList<PackIconLayer>>> DataIndex { get; }

        static PackIconLayerDataFactory()
        {
            DataIndex = new Lazy<ReadOnlyDictionary<TEnum, IReadOnlyList<PackIconLayer>>>(() => new ReadOnlyDictionary<TEnum, IReadOnlyList<PackIconLayer>>(Create()));
        }

        public static IDictionary<TEnum, IReadOnlyList<PackIconLayer>> Create()
        {
            try
            {
                using var iconJsonStream = AssetLoader.Open(new Uri($"avares://{typeof(TEnum).Assembly.GetName().Name}/Resources/IconLayers.json"));
                using var document = JsonDocument.Parse(iconJsonStream);
                var result = new Dictionary<TEnum, IReadOnlyList<PackIconLayer>>();
                foreach (var iconProperty in document.RootElement.EnumerateObject())
                {
                    if (!Enum.TryParse<TEnum>(iconProperty.Name, out var enumKey) || iconProperty.Value.ValueKind != JsonValueKind.Array)
                    {
                        continue;
                    }

                    var layers = new List<PackIconLayer>();
                    foreach (var item in iconProperty.Value.EnumerateArray())
                    {
                        if (!item.TryGetProperty("data", out var dataElement))
                        {
                            continue;
                        }

                        var data = dataElement.GetString();
                        if (string.IsNullOrWhiteSpace(data))
                        {
                            continue;
                        }

                        var opacity = 1d;
                        if (item.TryGetProperty("opacity", out var opacityElement) && opacityElement.TryGetDouble(out var parsedOpacity))
                        {
                            opacity = parsedOpacity;
                        }

                        opacity = opacity < 0d ? 0d : opacity > 1d ? 1d : opacity;
                        layers.Add(new PackIconLayer(StreamGeometry.Parse(data), opacity));
                    }

                    if (layers.Count > 0)
                    {
                        result[enumKey] = layers;
                    }
                }

                return result;
            }
            catch
            {
                return CreateFromPathData();
            }
        }

        private static IDictionary<TEnum, IReadOnlyList<PackIconLayer>> CreateFromPathData()
        {
            var dataIndex = PackIconDataFactory<TEnum>.DataIndex.Value;
            var result = new Dictionary<TEnum, IReadOnlyList<PackIconLayer>>(dataIndex.Count);
            foreach (var kvp in dataIndex)
            {
                result[kvp.Key] = new[] { new PackIconLayer(StreamGeometry.Parse(kvp.Value), 1d) };
            }

            return result;
        }
    }
}
""",
        encoding="utf-8")

    patch_text(
        core_dir / "PackIconControlBase.cs",
        [
            ("using System;\n", "using System;\nusing System.Collections.Generic;\n"),
            (
                """        public bool SpinAutoReverse
        {
            get { return this.GetValue(SpinAutoReverseProperty); }
            set { this.SetValue(SpinAutoReverseProperty, value); }
        }
""",
                """        public bool SpinAutoReverse
        {
            get { return this.GetValue(SpinAutoReverseProperty); }
            set { this.SetValue(SpinAutoReverseProperty, value); }
        }

        public static readonly StyledProperty<IReadOnlyList<PackIconLayer>> LayersProperty
            = AvaloniaProperty.Register<PackIconControlBase, IReadOnlyList<PackIconLayer>>(nameof(Layers));

        public IReadOnlyList<PackIconLayer> Layers
        {
            get { return this.GetValue(LayersProperty); }
            set { this.SetValue(LayersProperty, value); }
        }
""",
            ),
        ],
    )

    patch_text(
        core_dir / "PackIconControlBase.axaml",
        [
            (
                """                    <Path x:Name="PART_IconPath"
                          Data="{TemplateBinding Data}"
                          Stretch="Uniform"
                          Fill="{TemplateBinding Foreground}"
                          UseLayoutRounding="False" />
""",
                """                    <Grid>
                        <Path x:Name="PART_IconPath"
                              Data="{TemplateBinding Data}"
                              Stretch="Uniform"
                              Fill="{TemplateBinding Foreground}"
                              UseLayoutRounding="False" />
                        <iconPacks:PackIconLayerView Layers="{TemplateBinding Layers}"
                                                     Foreground="{TemplateBinding Foreground}" />
                    </Grid>
""",
            )
        ],
    )

    patch_text(
        mingcute_dir / "PackIconMingCuteIcons.cs",
        [
            (
                """        protected override void UpdateData()
        {
            if (Kind != default)
            {
                string data = null;
                PackIconDataFactory<PackIconMingCuteIconsKind>.DataIndex.Value?.TryGetValue(Kind, out data);
                this.Data = data != null ? StreamGeometry.Parse(data) : null;
            }
            else
            {
                this.Data = null;
            }
        }
""",
                """        protected override void UpdateData()
        {
            if (Kind != default)
            {
                IReadOnlyList<PackIconLayer> layers = null;
                PackIconLayerDataFactory<PackIconMingCuteIconsKind>.DataIndex.Value?.TryGetValue(Kind, out layers);
                this.Layers = layers;
                this.Data = null;
            }
            else
            {
                this.Layers = null;
                this.Data = null;
            }
        }
""",
            )
        ],
    )

    patch_text(
        mingcute_dir / "PackIconMingCuteIcons.cs",
        [("using Avalonia.Media;\n", "using System.Collections.Generic;\nusing Avalonia.Media;\n")],
    )


def load_icons() -> dict[str, tuple[str, str, list[dict[str, object]]]]:
    icons: dict[str, tuple[str, str, list[dict[str, object]]]] = {"None": ("Empty placeholder", "", [])}
    for svg_path in sorted(SVG_ROOT.glob("**/*.svg"), key=icon_sort_key):
        stem = svg_path.stem
        if not (stem.endswith("_fill") or stem.endswith("_line")):
            continue

        icon_name = pascal_icon_name(stem)
        layers = extract_icon_layers(svg_path)
        icons[icon_name] = (stem, " ".join(str(layer["data"]) for layer in layers), layers)

    return icons


def write_kind(kind_path: Path, icons: dict[str, tuple[str, str, list[dict[str, object]]]]) -> None:
    lines = [
        "using System.ComponentModel;",
        "",
        "namespace IconPacks.Avalonia.MingCuteIcons",
        "{",
        "    /// ******************************************",
        "    /// This code is auto generated. Do not amend.",
        "    /// ******************************************",
        "",
        "    /// <summary>",
        "    /// List of available icons for use with <see cref=\"PackIconMingCuteIcons\" />.",
        "    /// </summary>",
        "    /// <remarks>",
        "    /// MingCute Icons are licensed under Apache-2.0.",
        "    /// Source: https://github.com/mingcute-design/mingcute-icons",
        "    /// </remarks>",
        "    public enum PackIconMingCuteIconsKind",
        "    {",
    ]

    for index, (name, (description, _, _)) in enumerate(icons.items()):
        suffix = "," if index < len(icons) - 1 else ""
        lines.append(f"        [Description(\"{description}\")] {name}{suffix}")

    lines.extend(["    }", "}", ""])
    kind_path.write_text("\n".join(lines), encoding="utf-8")


def write_icons_json(json_path: Path, icons: dict[str, tuple[str, str, list[dict[str, object]]]]) -> None:
    data = {name: geometry for name, (_, geometry, _) in icons.items()}
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_icon_layers_json(json_path: Path, icons: dict[str, tuple[str, str, list[dict[str, object]]]]) -> None:
    data = {name: layers for name, (_, _, layers) in icons.items()}
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_iconpack(iconpacks_dir: Path) -> None:
    patch_layer_support(iconpacks_dir)
    project_dir = iconpacks_dir / "src" / "IconPacks.Avalonia.MingCuteIcons"
    resources_dir = project_dir / "Resources"
    resources_dir.mkdir(parents=True, exist_ok=True)

    icons = load_icons()
    print(f"生成 Avalonia MingCute 图标: {len(icons) - 1}")
    write_kind(project_dir / "PackIconMingCuteIconsKind.cs", icons)
    write_icons_json(resources_dir / "Icons.json", icons)
    write_icon_layers_json(resources_dir / "IconLayers.json", icons)


def find_nuspec(nupkg: Path) -> tuple[str, str, str]:
    with zipfile.ZipFile(nupkg) as package:
        nuspec_name = next(name for name in package.namelist() if name.endswith(".nuspec"))
        raw = package.read(nuspec_name).decode("utf-8")

    document = ET.fromstring(raw)
    ns = {"n": document.tag.split("}")[0].strip("{")} if document.tag.startswith("{") else {}
    metadata = document.find("n:metadata", ns) if ns else document.find("metadata")
    if metadata is None:
        raise RuntimeError(f"nupkg 缺少 metadata: {nupkg}")

    def text(name: str) -> str:
        element = metadata.find(f"n:{name}", ns) if ns else metadata.find(name)
        return (element.text or "").strip() if element is not None else ""

    return text("id"), text("version"), raw


def write_feed(nupkg_dir: Path, output_dir: Path, feed_base_url: str) -> None:
    feed_root = output_dir / "nuget" / "v3"
    if feed_root.exists():
        shutil.rmtree(feed_root)
    feed_root.mkdir(parents=True, exist_ok=True)

    base = feed_base_url.rstrip("/") + "/"
    packages: dict[str, list[tuple[str, Path, str]]] = {}
    for nupkg in sorted(nupkg_dir.glob("*.nupkg")):
        if nupkg.name.endswith(".symbols.nupkg"):
            continue
        package_id, version, nuspec = find_nuspec(nupkg)
        packages.setdefault(package_id.lower(), []).append((version.lower(), nupkg, nuspec))

    for package_id, versions in packages.items():
        versions.sort(key=lambda item: item[0])
        package_root = feed_root / "flatcontainer" / package_id
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "index.json").write_text(
            json.dumps({"versions": [version for version, _, _ in versions]}, indent=2) + "\n",
            encoding="utf-8")

        registration_items = []
        for version, nupkg, nuspec in versions:
            version_root = package_root / version
            version_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(nupkg, version_root / f"{package_id}.{version}.nupkg")
            (version_root / f"{package_id}.nuspec").write_text(nuspec, encoding="utf-8")

            package_url = f"{base}flatcontainer/{package_id}/{version}/{package_id}.{version}.nupkg"
            registration_items.append({
                "@id": f"{base}registration/{package_id}/{version}.json",
                "@type": "Package",
                "commitId": "00000000-0000-0000-0000-000000000000",
                "commitTimeStamp": "1970-01-01T00:00:00Z",
                "catalogEntry": {
                    "@id": f"{base}registration/{package_id}/{version}.json",
                    "@type": "PackageDetails",
                    "id": package_id,
                    "version": version,
                    "description": "IconPacks.Avalonia MingCute package built from mingcute-design/mingcute-icons.",
                    "authors": "MahApps;MingCute",
                    "licenseExpression": "MIT AND Apache-2.0",
                    "packageContent": package_url,
                },
                "packageContent": package_url,
            })

        registration_root = feed_root / "registration" / package_id
        registration_root.mkdir(parents=True, exist_ok=True)
        registration_index = {
            "@id": f"{base}registration/{package_id}/index.json",
            "@type": ["catalog:CatalogRoot", "PackageRegistration", "catalog:Permalink"],
            "count": 1,
            "items": [{
                "@id": f"{base}registration/{package_id}/index.json#page/0",
                "@type": "catalog:CatalogPage",
                "commitId": "00000000-0000-0000-0000-000000000000",
                "commitTimeStamp": "1970-01-01T00:00:00Z",
                "count": len(registration_items),
                "items": registration_items,
                "lower": versions[0][0],
                "upper": versions[-1][0],
            }],
        }
        (registration_root / "index.json").write_text(json.dumps(registration_index, indent=2) + "\n", encoding="utf-8")

    index = {
        "version": "3.0.0",
        "resources": [
            {"@id": f"{base}flatcontainer/", "@type": "PackageBaseAddress/3.0.0"},
            {"@id": f"{base}registration/", "@type": "RegistrationsBaseUrl/3.6.0"},
            {"@id": f"{base}registration/", "@type": "RegistrationsBaseUrl/3.0.0-beta"},
        ],
    }
    (feed_root / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    (output_dir / "index.html").write_text(
        f"<!doctype html><meta charset=\"utf-8\"><title>MingCute Avalonia NuGet Feed</title>"
        f"<p>NuGet feed: <code>{escape(base)}index.json</code></p>\n",
        encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare-iconpack")
    prepare_parser.add_argument("--iconpacks-dir", type=Path, required=True)

    feed_parser = subparsers.add_parser("write-feed")
    feed_parser.add_argument("--nupkg-dir", type=Path, required=True)
    feed_parser.add_argument("--output-dir", type=Path, required=True)
    feed_parser.add_argument("--feed-base-url", required=True)

    args = parser.parse_args()
    if args.command == "prepare-iconpack":
        prepare_iconpack(args.iconpacks_dir)
    elif args.command == "write-feed":
        write_feed(args.nupkg_dir, args.output_dir, args.feed_base_url)
    else:
        raise AssertionError(args.command)


if __name__ == "__main__":
    main()
