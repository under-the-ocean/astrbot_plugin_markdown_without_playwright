"""
astrbot_plugin_markdown_without_playwright - Markdown to PNG rendering plugin

Features:
  - Pure Python rendering pipeline, no browser required
  - Math formula support (KaTeX engine, $$ block / $ inline)
  - Tables, code blocks (syntax highlighting), quotes, links
  - Light/dark themes
  - Auto-intercept AI responses and convert to images
"""

from pathlib import Path

from astrbot.api.event import filter
from astrbot.api.star import Star
from astrbot.api.all import AstrMessageEvent
from astrbot.api.message_components import Image, Plain

from .core.engine import MarkdownRenderEngine


class MarkdownRenderPlugin(Star):
    """AstrBot plugin: Markdown to PNG image rendering."""

    def __init__(self, context: "Register"):
        super().__init__(context)
        self.config = getattr(context, "config", {}) or {}
        self._base_dir = Path(__file__).parent.resolve()
        self._resource_dir = self._base_dir / "resource"
        self._template_dir = self._base_dir / "templates"
        self._output_dir = self._base_dir / "data"
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._engine = MarkdownRenderEngine(
            resource_dir=self._resource_dir,
            template_dir=self._template_dir,
            output_dir=self._output_dir,
            math_engine=self.config.get("math_engine", "fallback"),
        )

    async def initialize(self):
        self._engine.initialize(
            auto_install_dependencies=self.config.get("auto_install_dependencies", True)
        )
        self._sync_conf_schema()
        print("[MarkdownRender] Engine warmed up")

    async def terminate(self):
        self._engine.terminate()
        if self._output_dir.exists():
            for file_path in self._output_dir.iterdir():
                if file_path.suffix == ".png":
                    file_path.unlink()
        print("[MarkdownRender] Engine shut down, temp files cleaned")

    # ---------------------------------------------------------------- #
    #  Auto-intercept: convert AI text responses to images              #
    # ---------------------------------------------------------------- #

    @filter.on_decorating_result()
    async def auto_render_hook(self, event: AstrMessageEvent):
        if not self.config.get("auto_render", True):
            return

        result = event.get_result()
        if result is None or not result.is_llm_result():
            return

        # Only handle pure-text LLM responses (no existing images/voice)
        if not result.chain or any(not isinstance(c, Plain) for c in result.chain):
            return

        md_text = " ".join(c.text for c in result.chain)
        if not md_text or not md_text.strip():
            return

        # Threshold & math-formula gating
        threshold = int(self.config.get("auto_render_threshold", 200))
        force_math = self.config.get("auto_render_math", True)
        over_threshold = len(md_text) > threshold
        has_math = "$$" in md_text or (md_text.count("$") >= 2)

        if not over_threshold and not (force_math and has_math):
            return

        theme = self._normalize_theme(self.config.get("default_theme", "default"))
        width = int(self.config.get("default_width", 800))
        font_name = self._normalize_font(self.config.get("default_font", "zh-cn.ttf"))

        img_path = self._engine.render(
            md_text,
            theme=theme,
            width=width,
            font_name=font_name,
        )
        if img_path is None or not img_path.exists():
            return

        result.chain = [Image.fromFileSystem(str(img_path))]
        result.use_t2i(False)

    # ---------------------------------------------------------------- #
    #  LLM tool / slash command handlers                                #
    # ---------------------------------------------------------------- #

    @filter.llm_tool("markdown_render")
    async def markdown_render(self, event: AstrMessageEvent, md_text: str, theme: str = "default"):
        theme = self._normalize_theme(theme or self.config.get("default_theme", "default"))
        width = int(self.config.get("default_width", 800))
        model_name = self.config.get("display_model_name", "")
        font_name = self._normalize_font(self.config.get("default_font", "zh-cn.ttf"))

        img_path = self._engine.render(
            md_text,
            theme=theme,
            width=width,
            model_name=model_name,
            font_name=font_name,
        )
        if img_path is None or not img_path.exists():
            err_path = self._write_error_image("Render failed. Check Markdown syntax.")
            if err_path:
                yield event.image_result(err_path)
            return

        yield event.image_result(str(img_path))

    @filter.command("render_md")
    async def render_md_command(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        prefix = "/render_md"
        md_text = msg[len(prefix):].strip() if msg.startswith(prefix) else msg

        if not md_text:
            err_path = self._write_error_image("Please provide Markdown text")
            if err_path:
                yield event.image_result(err_path)
            return

        theme = self.config.get("default_theme", "default")
        for candidate in self._engine.available_themes():
            flag = f" -{candidate}"
            if flag in md_text:
                theme = candidate
                md_text = md_text.replace(flag, "")
                break

        md_text = md_text.strip()
        theme = self._normalize_theme(theme)
        width = int(self.config.get("default_width", 800))
        model_name = self.config.get("display_model_name", "")
        font_name = self._normalize_font(self.config.get("default_font", "zh-cn.ttf"))

        img_path = self._engine.render(
            md_text,
            theme=theme,
            width=width,
            model_name=model_name,
            font_name=font_name,
        )
        if img_path is None or not img_path.exists():
            err_path = self._write_error_image("Render failed. Check Markdown syntax.")
            if err_path:
                yield event.image_result(err_path)
            return

        yield event.image_result(str(img_path))

    # ---------------------------------------------------------------- #
    #  Utilities                                                        #
    # ---------------------------------------------------------------- #

    def _write_error_image(self, text: str) -> str:
        from weasyprint import HTML

        html = f"""<!DOCTYPE html>
<html><meta charset=\"utf-8\">
<body style=\"font-family:sans-serif;padding:40px;color:#c00;background:#fff;font-size:18px;\">
<h2>{text}</h2>
</body></html>"""
        import uuid
        path = str(self._output_dir / f"_error_{uuid.uuid4().hex[:8]}.png")
        try:
            HTML(string=html).write_png(path)
            if Path(path).exists():
                return path
        except Exception as e:
            print(f"[MarkdownRender] Error image failed: {e}")
        return ""

    def _normalize_theme(self, theme: str) -> str:
        themes = self._engine.available_themes()
        if theme not in themes:
            return "default" if "default" in themes else themes[0]
        return theme

    def _normalize_font(self, font_name: str) -> str:
        fonts = self._engine.available_fonts()
        if font_name in fonts:
            return font_name
        if "zh-cn.ttf" in fonts:
            return "zh-cn.ttf"
        return fonts[0] if fonts else ""

    def _sync_conf_schema(self):
        schema_path = self._base_dir / "_conf_schema.json"
        themes = self._engine.available_themes()
        fonts = self._engine.available_fonts()
        default_theme = self._normalize_theme(self.config.get("default_theme", "default"))
        default_font = self._normalize_font(self.config.get("default_font", "zh-cn.ttf"))
        schema = {
            "auto_render": {
                "description": "自动拦截 AI 回复并转换为图片",
                "type": "bool",
                "hint": "开启后 AI 文本回复自动渲染为 PNG 图片发送",
                "default": True,
            },
            "auto_render_threshold": {
                "description": "自动转图片的字数阈值",
                "type": "int",
                "hint": "超过此字数的 AI 回复自动转换为图片（推荐 200）",
                "default": 200,
            },
            "auto_render_math": {
                "description": "含公式时强制转图片",
                "type": "bool",
                "hint": "AI 回复包含 $$ 数学公式时忽略字数阈值直接转图片",
                "default": True,
            },
            "math_engine": {
                "description": "数学公式渲染引擎",
                "type": "string",
                "hint": "fallback=纯文本(零依赖) | mini_racer=V8编译 | nodejs=Node.js子进程",
                "default": "fallback",
                "options": ["fallback", "mini_racer", "nodejs"],
            },
            "default_theme": {
                "description": "默认主题，自动扫描 templates 目录",
                "type": "string",
                "hint": "下拉选择默认渲染模板",
                "default": default_theme,
                "options": themes,
            },
            "default_font": {
                "description": "默认字体，自动扫描 resource 目录",
                "type": "string",
                "hint": "下拉选择默认字体文件",
                "default": default_font,
                "options": fonts,
            },
            "default_width": {
                "description": "图片渲染宽度，单位 px",
                "type": "int",
                "hint": "推荐 720-1200",
                "default": 800,
            },
            "display_model_name": {
                "description": "默认显示的模型名",
                "type": "string",
                "hint": "运行时拿不到模型名时使用",
                "default": "",
            },
            "auto_install_dependencies": {
                "description": "初始化时自动安装 Python 依赖",
                "type": "bool",
                "hint": "依赖由 requirements.txt 管理时可关闭",
                "default": True,
            },
        }
        import json
        schema_path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
