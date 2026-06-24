"""
core/engine.py - Markdown to PNG rendering engine

Pipeline:
  1. Extract $$...$$ and $...$ math formulas
  2. Render formulas to HTML via MiniRacer + KaTeX
  3. Convert Markdown to HTML (tables, code blocks, highlighting)
  4. Assemble full HTML with Jinja2 template + theme variables
  5. WeasyPrint renders HTML to PNG
"""

import importlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


class MarkdownRenderEngine:
    """Pure Python Markdown to PNG rendering engine, singleton reuse."""

    def __init__(self, resource_dir: Path, template_dir: Path, output_dir: Path):
        self.resource_dir = resource_dir
        self.template_dir = template_dir
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._auto_install_dependencies = True

        self._ctx = None

        self._ensure_python_dependencies()
        jinja2 = self._import_or_install("jinja2")
        self._jinja = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(template_dir)),
            autoescape=jinja2.select_autoescape(["html", "xml"]),
        )

        self._re_block = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
        self._re_inline = re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", re.DOTALL)

    def initialize(self, auto_install_dependencies: bool = True):
        self._auto_install_dependencies = auto_install_dependencies
        self._ensure_python_dependencies()
        self._ensure_file("katex.min.js")
        self._init_v8()

    def terminate(self):
        if self._ctx is not None:
            try:
                del self._ctx
            except Exception:
                pass
            self._ctx = None

    def available_themes(self) -> list[str]:
        return sorted(path.stem for path in self.template_dir.glob("*.html"))

    def available_fonts(self) -> list[str]:
        suffixes = {".ttf", ".otf", ".woff", ".woff2"}
        return sorted(
            path.name for path in self.resource_dir.iterdir()
            if path.is_file() and path.suffix.lower() in suffixes
        )

    def render(
        self,
        md_text: str,
        theme: str = "default",
        width: int = 800,
        model_name: str = "",
        total_tokens: Optional[int] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        render_time: Optional[datetime] = None,
        font_name: str = "zh-cn.ttf",
    ) -> Optional[Path]:
        if not md_text or not md_text.strip():
            return None

        processed_text, formula_map = self._extract_and_render_formulas(md_text)
        html_body = self._md_to_html(processed_text)

        for placeholder, html_fragment in formula_map.items():
            html_body = html_body.replace(placeholder, html_fragment, 1)

        full_html = self._apply_template(
            html_body,
            theme,
            width,
            model_name=model_name,
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            render_time=render_time,
            font_name=font_name,
        )
        return self._render_png(full_html, theme)

    def _extract_and_render_formulas(self, text: str) -> tuple:
        placeholders = {}

        def _replace_block(match):
            expr = match.group(1).strip()
            html = self._render_katex(expr, display_mode=True)
            placeholder = f"%%KATEX_BLOCK_{len(placeholders)}%%"
            placeholders[placeholder] = html
            return placeholder

        def _replace_inline(match):
            expr = match.group(1).strip()
            html = self._render_katex(expr, display_mode=False)
            placeholder = f"%%KATEX_INLINE_{len(placeholders)}%%"
            placeholders[placeholder] = html
            return placeholder

        text = self._re_block.sub(_replace_block, text)
        text = self._re_inline.sub(_replace_inline, text)
        return text, placeholders

    def _render_katex(self, expr: str, display_mode: bool) -> str:
        if self._ctx is None:
            self._ensure_file("katex.min.js")
            self._init_v8()

        safe_expr = (
            expr.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "\\n")
        )
        js = (
            f"katex.renderToString('{safe_expr}', "
            f"{{displayMode: {'true' if display_mode else 'false'}, throwOnError: false}})"
        )
        try:
            return str(self._ctx.eval(js))
        except Exception:
            return f'<span class="katex-error" style="color:red">{expr}</span>'

    def _md_to_html(self, text: str) -> str:
        markdown_module = self._import_or_install("markdown")
        return markdown_module.markdown(
            text,
            extensions=["extra", "codehilite", "sane_lists", "toc"],
            extension_configs={
                "codehilite": {
                    "css_class": "highlight",
                    "guess_lang": True,
                    "use_pygments": True,
                },
            },
        )

    def _apply_template(
        self,
        body: str,
        theme: str,
        width: int,
        model_name: str = "",
        total_tokens: Optional[int] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        render_time: Optional[datetime] = None,
        font_name: str = "zh-cn.ttf",
    ) -> str:
        template_name = f"{theme}.html"
        if not (self.template_dir / template_name).exists():
            template_name = "default.html"
        tpl = self._jinja.get_template(template_name)
        katex_style = self._load_file("katex.min.css")
        timestamp = (render_time or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

        font_path = self.resource_dir / font_name
        if not font_path.exists():
            fallback_path = self.resource_dir / "zh-cn.ttf"
            if fallback_path.exists():
                font_path = fallback_path
            else:
                font_candidates = self.available_fonts()
                if font_candidates:
                    font_path = self.resource_dir / font_candidates[0]
        font_url = font_path.as_uri()

        return tpl.render(
            content=body,
            width=width,
            katex_css=katex_style,
            font_url=font_url,
            render_time=timestamp,
            model_name=model_name,
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            show_meta=bool(
                model_name
                or total_tokens is not None
                or prompt_tokens is not None
                or completion_tokens is not None
            ),
        )

    def _render_png(self, html_str: str, theme: str) -> Optional[Path]:
        output_path = self.output_dir / f"render_{theme}.png"
        weasyprint = self._import_or_install("weasyprint")

        try:
            weasyprint.HTML(string=html_str, base_url=str(self.resource_dir)).write_png(
                str(output_path)
            )
            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path
        except Exception:
            pass

        try:
            pdf2image = self._import_or_install("pdf2image")
            pdf_bytes = weasyprint.HTML(
                string=html_str,
                base_url=str(self.resource_dir),
            ).write_pdf()
            images = pdf2image.convert_from_bytes(pdf_bytes, dpi=200)
            images[0].save(str(output_path), "PNG")
            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path
        except Exception as e:
            print(f"[MarkdownRenderEngine] PNG render failed: {e}", file=sys.stderr)
        return None

    def _ensure_file(self, filename: str):
        path = self.resource_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Required file not found: {path}")

    def _load_file(self, filename: str) -> str:
        path = self.resource_dir / filename
        if path.exists():
            with open(path, "r", encoding="utf-8") as file:
                return file.read()
        return ""

    def _init_v8(self):
        mini_racer_module = self._import_js_runtime()
        self._ctx = mini_racer_module.MiniRacer()
        katex_path = self.resource_dir / "katex.min.js"
        with open(katex_path, "r", encoding="utf-8") as file:
            js_code = file.read()
        self._ctx.eval(js_code)

    def _ensure_python_dependencies(self):
        for module_name, pip_name in (
            ("markdown", "Markdown"),
            ("jinja2", "Jinja2"),
            ("weasyprint", "weasyprint"),
            ("pdf2image", "pdf2image"),
        ):
            self._import_or_install(module_name, pip_name)
        self._import_js_runtime()

    def _import_js_runtime(self):
        try:
            return importlib.import_module("py_mini_racer")
        except Exception:
            if not self._auto_install_dependencies:
                raise
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "mini-racer"],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            return importlib.import_module("mini_racer")

    def _import_or_install(self, module_name: str, pip_name: Optional[str] = None):
        try:
            return importlib.import_module(module_name)
        except ImportError:
            if not self._auto_install_dependencies:
                raise
            package_name = pip_name or module_name
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", package_name],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            return importlib.import_module(module_name)
