"""
core/engine.py - Markdown to PNG rendering engine

Pipeline:
  1. Extract $$...$$ and $...$ math formulas
  2. Render formulas to HTML via configurable math engine
  3. Convert Markdown to HTML (tables, code blocks, highlighting)
  4. Assemble full HTML with Jinja2 template + theme variables
  5. WeasyPrint renders HTML to PNG

Math engines:
  - fallback:   Plain-text LaTeX source, no external dependency (default)
  - mini_racer: Python V8 binding (needs compiled native lib)
  - nodejs:     Node.js subprocess (needs `node` on PATH)
"""

import importlib
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


class MarkdownRenderEngine:
    """Pure Python Markdown to PNG rendering engine, singleton reuse."""

    MATH_ENGINES = ("fallback", "mini_racer", "nodejs")

    def __init__(
        self,
        resource_dir: Path,
        template_dir: Path,
        output_dir: Path,
    ):
        self.resource_dir = resource_dir
        self.template_dir = template_dir
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._auto_install_dependencies = True
        self._math_engine = "fallback"  # set in initialize()

        self._ctx = None
        self._math_available = False

        # Node.js subprocess paths
        self._node_bin = "node"
        self._node_script_path: Optional[Path] = None

        self._ensure_python_dependencies()
        jinja2 = self._import_or_install("jinja2")
        self._jinja = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(template_dir)),
            autoescape=jinja2.select_autoescape(["html", "xml"]),
        )

        self._re_block = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
        self._re_inline = re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", re.DOTALL)

    def initialize(self, auto_install_dependencies: bool = True, math_engine="fallback"):
        self._math_engine = self._normalize_math_engine(math_engine)
        self._auto_install_dependencies = auto_install_dependencies
        self._ensure_python_dependencies()
        self._ensure_file("katex.min.js")
        self._init_math_engine()

    def terminate(self):
        if self._ctx is not None:
            try:
                del self._ctx
            except Exception:
                pass
            self._ctx = None
        if self._node_script_path is not None and self._node_script_path.exists():
            try:
                self._node_script_path.unlink()
            except Exception:
                pass

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
            model_name=model_name,
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            render_time=render_time,
            font_name=font_name,
        )
        return self._render_png(full_html, theme)

    # ------------------------------------------------------------------ #
    #  Math engine dispatch                                               #
    # ------------------------------------------------------------------ #

    def _init_math_engine(self):
        print(f"[MarkdownRenderEngine] Initializing math engine: {self._math_engine}", file=sys.stderr)
        if self._math_engine == "mini_racer":
            self._init_mini_racer()
        elif self._math_engine == "nodejs":
            self._init_nodejs()
        else:
            self._init_fallback()
        print(f"[MarkdownRenderEngine] Math engine ready: available={self._math_available}", file=sys.stderr)

    def _render_katex(self, expr: str, display_mode: bool) -> str:
        if self._math_engine == "mini_racer":
            if not self._math_available:
                print("[MarkdownRenderEngine] mini_racer unavailable, falling back to plain text", file=sys.stderr)
            return self._render_katex_v8(expr, display_mode)
        elif self._math_engine == "nodejs":
            if not self._math_available:
                print("[MarkdownRenderEngine] nodejs unavailable, falling back to plain text", file=sys.stderr)
            return self._render_katex_nodejs(expr, display_mode)
        else:
            print("[MarkdownRenderEngine] Using fallback (plain-text LaTeX)", file=sys.stderr)
            return self._render_katex_fallback(expr, display_mode)

    # ------------------------------------------------------------------ #
    #  mini_racer (V8) mode                                               #
    # ------------------------------------------------------------------ #

    def _init_mini_racer(self):
        try:
            mini_racer_module = self._import_js_runtime()
            self._ctx = mini_racer_module.MiniRacer()
            katex_path = self.resource_dir / "katex.min.js"
            with open(katex_path, "r", encoding="utf-8") as file:
                js_code = file.read()
            self._ctx.eval(js_code)
            self._math_available = True
            print("[MarkdownRenderEngine] mini_racer initialized OK (V8+KaTeX ready)", file=sys.stderr)
        except RuntimeError as exc:
            print(
                f"[MarkdownRenderEngine] V8 runtime unavailable ({exc}); "
                f"math rendering degraded",
                file=sys.stderr,
            )
            self._ctx = None
            self._math_available = False

    def _render_katex_v8(self, expr: str, display_mode: bool) -> str:
        if not self._math_available:
            return self._render_katex_fallback(expr, display_mode)

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

    # ------------------------------------------------------------------ #
    #  Node.js subprocess mode                                            #
    # ------------------------------------------------------------------ #

    def _init_nodejs(self):
        # Check Node.js availability
        try:
            subprocess.run(
                [self._node_bin, "--version"],
                capture_output=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print(
                "[MarkdownRenderEngine] `node` not found on PATH; "
                "math rendering degraded",
                file=sys.stderr,
            )
            self._math_available = False
            return

        # Write the JS helper script to temp location
        self._node_script_path = self.output_dir / "_katex_render.js"
        js_code = self._generate_node_script()
        self._node_script_path.write_text(js_code, encoding="utf-8")

        # Smoke-test
        test_expr = "a+b"
        result = self._call_node_katex(test_expr, display_mode=False)
        if result is None:
            print(
                "[MarkdownRenderEngine] Node.js KaTeX execution failed; "
                "math rendering degraded",
                file=sys.stderr,
            )
            self._math_available = False
        else:
            self._math_available = True

    def _generate_node_script(self) -> str:
        """Generate a self-contained Node.js script that renders KaTeX formulas.

        The script loads katex.min.js as a string and evaluates it in a sandboxed
        VM context.  It expects three arguments: katex_path, formula, display_mode.
        """
        return """// Auto-generated KaTeX render helper - do not edit
const fs = require('fs');
const vm = require('vm');

const [,, katexPath, formula, displayModeRaw] = process.argv;
const displayMode = displayModeRaw === 'true';

const code = fs.readFileSync(katexPath, 'utf-8');

// katex.min.js is a UMD/global bundle; evaluate in a fresh sandbox
const sandbox = { katex: null, console: console };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

if (typeof sandbox.katex !== 'object' || typeof sandbox.katex.renderToString !== 'function') {
    // Retry with a bare context (some builds attach to `this`)
    const alt = {};
    vm.createContext(alt);
    vm.runInContext(code, alt, { timeout: 5000 });
    sandbox.katex = alt.katex;
}

if (typeof sandbox.katex !== 'object' || typeof sandbox.katex.renderToString !== 'function') {
    console.error('KaTeX module not found after loading');
    process.exit(1);
}

try {
    const html = sandbox.katex.renderToString(formula, {
        displayMode: displayMode,
        throwOnError: false,
    });
    console.log(html);
} catch (e) {
    console.error(e.message);
    process.exit(1);
}
"""

    def _call_node_katex(self, expr: str, display_mode: bool) -> Optional[str]:
        """Invoke the Node.js KaTeX helper.  Returns HTML on success, None on failure."""
        if self._node_script_path is None or not self._node_script_path.exists():
            return None

        katex_abs = str(self.resource_dir / "katex.min.js")
        try:
            result = subprocess.run(
                [
                    self._node_bin,
                    str(self._node_script_path),
                    katex_abs,
                    expr,
                    "true" if display_mode else "false",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return None
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _render_katex_nodejs(self, expr: str, display_mode: bool) -> str:
        if not self._math_available:
            return self._render_katex_fallback(expr, display_mode)

        html = self._call_node_katex(expr, display_mode)
        if html is not None:
            return html

        # Subprocess call failed - degrade to plain text for this formula
        return f'<span class="katex-error" style="color:red">{expr}</span>'

    # ------------------------------------------------------------------ #
    #  Fallback mode (plain-text LaTeX source)                            #
    # ------------------------------------------------------------------ #

    def _init_fallback(self):
        self._math_available = False

    def _render_katex_fallback(self, expr: str, display_mode: bool) -> str:
        tag = "div" if display_mode else "span"
        cls = (
            "katex-fallback katex-fallback--block"
            if display_mode
            else "katex-fallback katex-fallback--inline"
        )
        return f'<{tag} class="{cls}">{expr}</{tag}>'

    # ------------------------------------------------------------------ #
    #  Markdown -> HTML & template assembly                                #
    # ------------------------------------------------------------------ #

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
            doc = weasyprint.HTML(
                string=html_str,
                base_url=str(self.resource_dir),
            ).render()
            doc.write_png(str(output_path))
            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path
        except Exception as e:
            print(f"[MarkdownRenderEngine] PNG render failed: {e}", file=sys.stderr)
        return None

    # ------------------------------------------------------------------ #
    #  Utilities                                                          #
    # ------------------------------------------------------------------ #

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

    def _ensure_python_dependencies(self):
        for module_name, pip_name in (
            ("markdown", "Markdown"),
            ("jinja2", "Jinja2"),
            ("weasyprint", "weasyprint"),
        ):
            self._import_or_install(module_name, pip_name)
        if self._math_engine == "mini_racer":
            self._import_js_runtime()

    def _import_js_runtime(self):
        try:
            return importlib.import_module("py_mini_racer")
        except ImportError:
            if not self._auto_install_dependencies:
                raise
            if platform.machine() in ("aarch64", "arm64"):
                print(
                    "[MarkdownRenderEngine] ARM64 detected, installing akracer",
                    file=sys.stderr,
                )
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "-q", "akracer"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "-q", "mini-racer"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return importlib.import_module("py_mini_racer")

    def _import_or_install(self, module_name: str, pip_name: Optional[str] = None):
        try:
            return importlib.import_module(module_name)
        except ImportError:
            if not self._auto_install_dependencies:
                raise
            package_name = pip_name or module_name
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-q", package_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return importlib.import_module(module_name)

    def _normalize_math_engine(self, value: str) -> str:
        value = value.strip().lower()
        if value not in self.MATH_ENGINES:
            print(
                f"[MarkdownRenderEngine] Unknown math_engine '{value}'; "
                f"falling back to 'fallback'",
                file=sys.stderr,
            )
            return "fallback"
        return value
