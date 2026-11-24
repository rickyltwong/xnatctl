import importlib.metadata


project = "xnatctl"
author = "Ricky Wong"
copyright = "2026, Ricky Wong"
release = importlib.metadata.version("xnatctl")
version = ".".join(release.split(".")[:2])


extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "myst_parser",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "plans", "README.md"]


autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented"


napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_attr_annotations = True


intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "click": ("https://click.palletsprojects.com/en/stable/", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
}


source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]


html_theme = "furo"
html_title = f"xnatctl {release}"

html_theme_options = {
    "source_repository": "https://github.com/rickyltwong/xnatctl",
    "source_branch": "main",
    "source_directory": "docs/",
    "navigation_with_keys": True,
}


copybutton_prompt_text = r"^\$ "
copybutton_prompt_is_regexp = True
