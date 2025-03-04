"""Simple streamlit app for interactive parsing and drawing."""
import io
import json
import gzip
import base64
import zipfile
import tempfile
import fnmatch
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, quote_from_bytes, unquote_to_bytes
from urllib.request import urlopen
from urllib.error import HTTPError
from importlib.metadata import version

import yaml
from cairosvg import svg2png  # type: ignore
import timeout_decorator

from keymap_drawer.draw import KeymapDrawer
from keymap_drawer.config import Config, DrawConfig, ParseConfig
from keymap_drawer.parse import QmkJsonParser, ZmkKeymapParser

import streamlit as st


LAYOUT_PREAMBLE = """\
# FILL IN below field with a value like {qmk_keyboard: ferris/sweep}
# or {ortho_layout: {split: true, rows: 3, columns: 5, thumbs: 2}}
# see https://github.com/caksoylar/keymap-drawer/blob/main/KEYMAP_SPEC.md#layout
#layout:
"""
APP_URL = "https://caksoylar.github.io/keymap-drawer"
REPO_REF = f"v{version('keymap_drawer')}"

DRAW_TIMEOUT = 10
PARSE_TIMEOUT = 30


def svg_to_png(svg_string: str, dark_bg: bool = False) -> bytes:
    """
    Convert SVG string in SVG/XML format to PNG using cairosvg, removing the unsupported stroke style for layer headers.
    """
    # remove white outline from layer headers, since we know the background color
    input_svg = svg_string.replace("stroke: white;", "")

    # change layer header to white if on black background
    if dark_bg:
        input_svg = input_svg.replace("text.label {", "text.label { fill: white;")

    # force text font to DejaVu Sans Mono, since cairosvg does not properly use font-family attribute
    input_svg = input_svg.replace("font-family: ", "font-family: DejaVu Sans Mono,")

    # remove relative font size specifiers since cairosvg can't handle them
    input_svg = re.sub(r'style="font-size: \d+(\.\d+)?%"', "", input_svg)

    return svg2png(bytestring=input_svg.encode("utf-8"), background_color="black" if dark_bg else "white")


@timeout_decorator.timeout(DRAW_TIMEOUT, use_signals=False)
def draw(yaml_str: str, config: DrawConfig) -> str:
    """Given a YAML keymap string, draw the keymap in SVG format to a string."""
    yaml_data = yaml.safe_load(yaml_str)
    assert "layers" in yaml_data, 'Keymap needs to be specified via the "layers" field in keymap YAML'
    assert "layout" in yaml_data, 'Physical layout needs to be specified via the "layout" field in keymap YAML'

    if custom_config := yaml_data.get("draw_config"):
        config = config.copy(update=custom_config)

    with io.StringIO() as out:
        drawer = KeymapDrawer(
            config=config,
            out=out,
            layers=yaml_data["layers"],
            layout=yaml_data["layout"],
            combos=yaml_data.get("combos", []),
        )
        drawer.print_board()
        return out.getvalue()


@st.cache_resource
def get_example_yamls() -> dict[str, str]:
    """Return mapping of example keymap YAML names to contents."""
    repo_zip = _download_zip("caksoylar", "keymap-drawer", REPO_REF)
    with zipfile.ZipFile(io.BytesIO(repo_zip)) as zipped:
        files = zipped.namelist()
        example_paths = sorted([Path(path) for path in files if fnmatch.fnmatch(path, "*/examples/*.yaml")])
        if not example_paths:
            raise RuntimeError("Retrying examples failed, please refresh the page :(")
        return {path.name: zipped.read(path.as_posix()).decode("utf-8") for path in example_paths}


@st.cache_data
def get_default_config() -> str:
    """Get and dump default config."""

    def cfg_str_representer(dumper, in_str):
        if "\n" in in_str:  # use '|' style for multiline strings
            return dumper.represent_scalar("tag:yaml.org,2002:str", in_str, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", in_str)

    yaml.representer.SafeRepresenter.add_representer(str, cfg_str_representer)
    return yaml.safe_dump(Config().dict(), sort_keys=False, allow_unicode=True)


@st.cache_data(max_entries=16)
def parse_config(config: str) -> Config:
    """Parse config from YAML format."""
    return Config.parse_obj(yaml.safe_load(config))


@timeout_decorator.timeout(PARSE_TIMEOUT, use_signals=False)
def parse_qmk_to_yaml(qmk_keymap_buf: io.BytesIO, config: ParseConfig, num_cols: int) -> str:
    """Parse a given QMK keymap JSON (buffer) into keymap YAML."""
    parsed = QmkJsonParser(config, num_cols).parse(io.TextIOWrapper(qmk_keymap_buf, encoding="utf-8"))
    return yaml.safe_dump(parsed, width=160, sort_keys=False, default_flow_style=None, allow_unicode=True)


@timeout_decorator.timeout(PARSE_TIMEOUT, use_signals=False)
def parse_zmk_to_yaml(zmk_keymap: Path | io.BytesIO, config: ParseConfig, num_cols: int, layout: str) -> str:
    """Parse a given ZMK keymap file (file path or buffer) into keymap YAML."""
    with open(zmk_keymap, encoding="utf-8") if isinstance(zmk_keymap, Path) else io.TextIOWrapper(
        zmk_keymap, encoding="utf-8"
    ) as keymap_buf:
        parsed = ZmkKeymapParser(config, num_cols).parse(keymap_buf)
    if layout:  # assign or override layout field if provided in app
        parsed["layout"] = json.loads(layout)  # pylint: disable=unsupported-assignment-operation

    out = yaml.safe_dump(parsed, width=160, sort_keys=False, default_flow_style=None, allow_unicode=True)
    if "layout" not in parsed:  # pylint: disable=unsupported-membership-test
        return LAYOUT_PREAMBLE + out
    return out


def _get_zmk_ref(owner: str, repo: str, head: str) -> str:
    try:
        with urlopen(f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{head}") as resp:
            sha = json.load(resp)["object"]["sha"]
    except HTTPError:
        # assume we are provided with a reference directly, like a commit SHA
        sha = head
    return sha


@st.cache_data(ttl=1800, max_entries=64)
def _download_zip(owner: str, repo: str, sha: str) -> bytes:
    """Use `sha` only used for caching purposes to make sure we are fetching from the same repo state."""
    zip_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{sha}"
    with urlopen(zip_url) as f:
        return f.read()


def _extract_zip_and_parse(
    zip_bytes: bytes, keymap_path: PurePosixPath, config: ParseConfig, num_cols: int, layout: str
) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zipped:
            zipped.extractall(tmpdir)
        keymap_file = next(path for path in Path(tmpdir).iterdir() if path.is_dir()) / keymap_path
        if not keymap_file.exists():
            raise ValueError(f"Could not find '{keymap_path}' in the repo, please check URL")
        return parse_zmk_to_yaml(keymap_file, config, num_cols, layout)


def parse_zmk_url_to_yaml(zmk_url: str, config: ParseConfig, num_cols: int, layout: str) -> str:
    """
    Parse a given ZMK keymap URL on Github into keymap YAML. Normalize URL, extract owner/repo/head name,
    get reference (not cached), download contents from reference (cached) and parse keymap (cached).
    """
    if not zmk_url.startswith("https") and not zmk_url.startswith("//"):
        zmk_url = "//" + zmk_url
    split_url = urlsplit(zmk_url, scheme="https")
    path = PurePosixPath(split_url.path)
    assert split_url.netloc.lower() == "github.com", "Please provide a Github URL"
    assert path.parts[3] == "blob", "Please provide URL for a file"
    assert path.parts[-1].endswith(".keymap"), "Please provide URL to a .keymap file"

    owner, repo, head = path.parts[1], path.parts[2], path.parts[4]
    keymap_path = PurePosixPath(*path.parts[5:])

    sha = _get_zmk_ref(owner, repo, head)
    zip_bytes = _download_zip(owner, repo, sha)
    return _extract_zip_and_parse(zip_bytes, keymap_path, config, num_cols, layout)


def get_permalink(keymap_yaml: str) -> str:
    """Encode a keymap using a compressed base64 string and place it in query params to create a permalink."""
    b64_bytes = base64.b64encode(gzip.compress(keymap_yaml.encode("utf-8"), mtime=0), altchars=b"-_")
    return f"{APP_URL}?keymap_yaml={quote_from_bytes(b64_bytes)}"


def decode_permalink_param(param: str) -> str:
    """Get a compressed base64 string from query params and decode it to keymap YAML."""
    return gzip.decompress(base64.b64decode(unquote_to_bytes(param), altchars=b"-_")).decode("utf-8")


def _handle_exception(container, message: str, exc: Exception):
    container.error(icon="❗", body=message)
    container.exception(exc)


def _set_state(arg: str, value: bool = True):
    st.session_state[arg] = value


def main():
    """Lay out Streamlit elements and widgets, run parsing and drawing logic."""
    st.set_page_config(page_title="Keymap Drawer live demo", page_icon=":keyboard:", layout="wide")
    st.write('<style>textarea[class^="st-"] { font-family: monospace; }</style>', unsafe_allow_html=True)
    st.header("`keymap-drawer` interactive demo")
    st.subheader("A visualizer for keyboard keymaps")
    st.caption(
        "Check out the documentation and Python CLI tool in the "
        "[GitHub repo](https://github.com/caksoylar/keymap-drawer)! "
        f"`keymap-drawer` version: {REPO_REF}"
    )

    examples = get_example_yamls()
    if not st.session_state.get("kd_config"):
        st.session_state.kd_config = get_default_config()
    if "keymap_yaml" not in st.session_state:
        st.session_state.keymap_yaml = examples[list(examples)[0]]

    query_params = st.experimental_get_query_params()
    if st.session_state.get("user_query", True):
        if "keymap_yaml" in query_params:
            st.session_state.keymap_yaml = decode_permalink_param(query_params["keymap_yaml"][0])
            st.experimental_set_query_params()
        st.session_state.example_yaml = query_params.get("example_yaml", [list(examples)[0]])[0]
        st.session_state.qmk_cols = query_params.get("num_cols", [0])[0]
        st.session_state.zmk_cols = query_params.get("num_cols", [0])[0]
        st.session_state.zmk_url = query_params.get("zmk_url", [""])[0]

    col_ex, col_qmk, col_zmk = st.columns(3)
    error_placeholder = st.empty()
    with col_ex:
        with st.expander("Example keymaps", expanded=st.session_state.get("example_expanded", False)):
            with st.form("example_form"):
                st.selectbox(label="Load example", options=list(examples), index=0, key="example_yaml")
                example_submitted = st.form_submit_button(
                    label="Show!", on_click=_set_state, args=["example_expanded"], use_container_width=True
                )
                if example_submitted or st.session_state.get("user_query", True) and "example_yaml" in query_params:
                    if example_submitted:
                        st.experimental_set_query_params(example_yaml=st.session_state.example_yaml)
                    st.session_state.keymap_yaml = examples[st.session_state.example_yaml]
    with col_qmk:
        with st.expander("Parse from QMK keymap", expanded=st.session_state.get("qmk_expanded", False)):
            with st.form("qmk_form"):
                num_cols = st.number_input(
                    "Number of columns in keymap (optional)", min_value=0, max_value=20, key="qmk_cols"
                )
                qmk_file = st.file_uploader(label="Import QMK `keymap.json`", type=["json"])
                qmk_submitted = st.form_submit_button(
                    label="Parse!", on_click=_set_state, args=["qmk_expanded"], use_container_width=True
                )
                if qmk_submitted:
                    if not qmk_file:
                        st.error(icon="❗", body="Please upload a keymap file")
                    else:
                        try:
                            st.session_state.keymap_yaml = parse_qmk_to_yaml(
                                qmk_file, parse_config(st.session_state.kd_config).parse_config, num_cols
                            )
                        except Exception as err:
                            _handle_exception(error_placeholder, "Error while parsing QMK keymap", err)
    with col_zmk:
        with st.expander("Parse from ZMK keymap", expanded=st.session_state.get("zmk_expanded", False)):
            with st.form("zmk_form"):
                num_cols = st.number_input(
                    "Number of columns in keymap (optional)", min_value=0, max_value=20, key="zmk_cols"
                )
                zmk_file = st.file_uploader(label="Import a ZMK `<keyboard>.keymap` file", type=["keymap"])
                zmk_file_submitted = st.form_submit_button(
                    label="Parse from file!", on_click=_set_state, args=["zmk_expanded"], use_container_width=True
                )
                if zmk_file_submitted:
                    if not zmk_file:
                        st.error(icon="❗", body="Please upload a keymap file")
                    else:
                        try:
                            st.session_state.keymap_yaml = parse_zmk_to_yaml(
                                zmk_file,
                                parse_config(st.session_state.kd_config).parse_config,
                                num_cols,
                                query_params.get("layout", [""])[0],
                            )
                        except Exception as err:
                            _handle_exception(error_placeholder, "Error while parsing ZMK keymap", err)

                st.text_input(
                    label="or, input GitHub URL to keymap",
                    placeholder="https://github.com/caksoylar/zmk-config/blob/main/config/hypergolic.keymap",
                    key="zmk_url",
                )
                zmk_url_submitted = st.form_submit_button(
                    label="Parse from URL!", on_click=_set_state, args=["zmk_expanded"], use_container_width=True
                )
                if zmk_url_submitted or st.session_state.get("user_query", True) and "zmk_url" in query_params:
                    if zmk_url_submitted:
                        st.experimental_set_query_params(zmk_url=st.session_state.zmk_url)
                    if not st.session_state.zmk_url:
                        st.error(icon="❗", body="Please enter a URL")
                    else:
                        try:
                            st.session_state.keymap_yaml = parse_zmk_url_to_yaml(
                                st.session_state.zmk_url,
                                parse_config(st.session_state.kd_config).parse_config,
                                num_cols,
                                query_params.get("layout", [""])[0],
                            )
                        except HTTPError as err:
                            _handle_exception(
                                error_placeholder,
                                "Could not get repo contents, make sure you use a branch name"
                                " or commit SHA and not a tag in the URL",
                                err,
                            )
                        except Exception as err:
                            _handle_exception(error_placeholder, "Error while parsing ZMK keymap from URL", err)

                st.caption("Please check and if necessary correct the `layout` field after parsing")

    keymap_col, draw_col = st.columns(2)
    with keymap_col:
        st.subheader("Keymap YAML")
        st.text_area(
            height=800,
            key="keymap_yaml",
            label="[Keymap Spec](https://github.com/caksoylar/keymap-drawer/blob/main/KEYMAP_SPEC.md)",
        )

        st.download_button(label="Download keymap", data=st.session_state.keymap_yaml, file_name="my_keymap.yaml")
        permabutton = st.button(label="Get permalink to keymap")
        if permabutton:
            st.code(get_permalink(st.session_state.keymap_yaml), language=None)

    with draw_col:
        try:
            svg = draw(st.session_state.keymap_yaml, parse_config(st.session_state.kd_config).draw_config)
            st.subheader("Keymap SVG")
            st.image(svg)

            with st.expander("Export"):
                svg_col, png_col = st.columns(2)
                with svg_col:
                    st.subheader("SVG")
                    st.download_button(label="Download", data=svg, file_name="my_keymap.svg")

                with png_col:
                    st.subheader("PNG")
                    st.caption(
                        "Note: Export might not render emojis and unicode characters as well as your browser and uses a fixed text font"
                    )
                    png_bg = st.radio("Background", ("White", "Black"))
                    st.download_button(
                        label="Export", data=svg_to_png(svg, png_bg == "Black"), file_name="my_keymap.png"
                    )
        except yaml.YAMLError as err:
            _handle_exception(st, "Could not parse keymap YAML, please check for syntax errors", err)
        except Exception as err:
            _handle_exception(st, "Error while drawing SVG from keymap YAML", err)

    with st.expander("Configuration", expanded=True):
        st.text_area(
            label="[Config parameters](https://github.com/caksoylar/keymap-drawer/blob/main/keymap_drawer/config.py)",
            key="kd_config",
            height=400,
        )
        st.download_button(label="Download config", data=st.session_state.kd_config, file_name="my_config.yaml")

    st.session_state.user_query = False


main()
