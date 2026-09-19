import importlib.abc
import importlib.machinery
import importlib.util
import os
import platform
import shutil
import sys
import tempfile
import time
import weakref

import pytest

from jinja2 import Environment
from jinja2 import loaders
from jinja2 import PackageLoader
from jinja2.exceptions import TemplateNotFound
from jinja2.loaders import split_template_path


class TestLoaders:
    def test_dict_loader(self, dict_loader):
        env = Environment(loader=dict_loader)
        tmpl = env.get_template("justdict.html")
        assert tmpl.render().strip() == "FOO"
        pytest.raises(TemplateNotFound, env.get_template, "missing.html")

    def test_package_loader(self, package_loader):
        env = Environment(loader=package_loader)
        tmpl = env.get_template("test.html")
        assert tmpl.render().strip() == "BAR"
        pytest.raises(TemplateNotFound, env.get_template, "missing.html")

    def test_filesystem_loader_overlapping_names(self, filesystem_loader):
        res = os.path.dirname(filesystem_loader.searchpath[0])
        t2_dir = os.path.join(res, "templates2")
        # Make "foo" show up before "foo/test.html".
        filesystem_loader.searchpath.insert(0, t2_dir)
        e = Environment(loader=filesystem_loader)
        e.get_template("foo")
        # This would raise NotADirectoryError if "t2/foo" wasn't skipped.
        e.get_template("foo/test.html")

    def test_choice_loader(self, choice_loader):
        env = Environment(loader=choice_loader)
        tmpl = env.get_template("justdict.html")
        assert tmpl.render().strip() == "FOO"
        tmpl = env.get_template("test.html")
        assert tmpl.render().strip() == "BAR"
        pytest.raises(TemplateNotFound, env.get_template, "missing.html")

    def test_function_loader(self, function_loader):
        env = Environment(loader=function_loader)
        tmpl = env.get_template("justfunction.html")
        assert tmpl.render().strip() == "FOO"
        pytest.raises(TemplateNotFound, env.get_template, "missing.html")

    def test_prefix_loader(self, prefix_loader):
        env = Environment(loader=prefix_loader)
        tmpl = env.get_template("a/test.html")
        assert tmpl.render().strip() == "BAR"
        tmpl = env.get_template("b/justdict.html")
        assert tmpl.render().strip() == "FOO"
        pytest.raises(TemplateNotFound, env.get_template, "missing")

    def test_caching(self):
        changed = False

        class TestLoader(loaders.BaseLoader):
            def get_source(self, environment, template):
                return "foo", None, lambda: not changed

        env = Environment(loader=TestLoader(), cache_size=-1)
        tmpl = env.get_template("template")
        assert tmpl is env.get_template("template")
        changed = True
        assert tmpl is not env.get_template("template")
        changed = False

    def test_no_cache(self):
        mapping = {"foo": "one"}
        env = Environment(loader=loaders.DictLoader(mapping), cache_size=0)
        assert env.get_template("foo") is not env.get_template("foo")

    def test_limited_size_cache(self):
        mapping = {"one": "foo", "two": "bar", "three": "baz"}
        loader = loaders.DictLoader(mapping)
        env = Environment(loader=loader, cache_size=2)
        t1 = env.get_template("one")
        t2 = env.get_template("two")
        assert t2 is env.get_template("two")
        assert t1 is env.get_template("one")
        env.get_template("three")
        loader_ref = weakref.ref(loader)
        assert (loader_ref, "one") in env.cache
        assert (loader_ref, "two") not in env.cache
        assert (loader_ref, "three") in env.cache

    def test_cache_loader_change(self):
        loader1 = loaders.DictLoader({"foo": "one"})
        loader2 = loaders.DictLoader({"foo": "two"})
        env = Environment(loader=loader1, cache_size=2)
        assert env.get_template("foo").render() == "one"
        env.loader = loader2
        assert env.get_template("foo").render() == "two"

    def test_dict_loader_cache_invalidates(self):
        mapping = {"foo": "one"}
        env = Environment(loader=loaders.DictLoader(mapping))
        assert env.get_template("foo").render() == "one"
        mapping["foo"] = "two"
        assert env.get_template("foo").render() == "two"

    def test_cached_template_globals_first_load(self):
        env = Environment(loader=loaders.DictLoader({"foo": "{{ value }}"}))
        tmpl = env.get_template("foo", globals={"value": "one"})
        assert tmpl.render() == "one"
        # cache hit, same object, globals are template specific
        assert env.get_template("foo") is tmpl

    def test_cached_template_globals_update(self):
        env = Environment(loader=loaders.DictLoader({"foo": "{{ value }}"}))
        tmpl = env.get_template("foo", globals={"value": "one"})
        assert tmpl.render() == "one"
        # loading again with new globals updates the cached template
        assert env.get_template("foo", globals={"value": "two"}) is tmpl
        assert tmpl.render() == "two"
        # a later plain load keeps the previously merged globals
        assert env.get_template("foo") is tmpl
        assert tmpl.render() == "two"

    def test_cached_template_globals_dont_pollute_environment(self):
        env = Environment(
            loader=loaders.DictLoader(
                {"foo": "{{ value }}", "bar": "{{ value }}"}
            )
        )
        env.globals["value"] = "env"
        tmpl = env.get_template("foo", globals={"value": "one"})
        assert tmpl.render() == "one"
        assert env.get_template("foo", globals={"value": "two"}) is tmpl
        assert tmpl.render() == "two"
        assert env.globals["value"] == "env"
        # other templates still see the environment global unchanged
        other = env.get_template("bar")
        assert other.render() == "env"
        assert env.get_template("bar") is other

    def test_cached_template_globals_caller_overrides(self):
        env = Environment(loader=loaders.DictLoader({"foo": "{{ value }}"}))
        env.globals["value"] = "env"
        first = env.get_template("foo", globals={"value": "one"})
        assert first.render() == "one"
        # another caller overrides the same cached template
        second = env.get_template("foo", globals={"value": "two"})
        assert second is first
        assert second.render() == "two"
        assert first.render() == "two"
        assert env.globals["value"] == "env"

    @pytest.mark.parametrize("tag", ["include", "import"])
    def test_cached_template_globals_include_import(self, tag):
        if tag == "include":
            main = "{% include 'base' %}"
            base = "{{ value }}"
        else:
            main = "{% import 'base' as base %}{{ base.test() }}"
            base = "{% macro test() %}{{ value }}{% endmacro %}"
        env = Environment(
            loader=loaders.DictLoader({"base": base, "main": main})
        )
        if tag == "include":
            env.globals["value"] = "env"

        # template globals passed to the caller are visible to the
        # indirectly loaded base template on first load
        tmpl = env.get_template("main", globals={"value": "one"})
        assert tmpl.render() == "one"

        # base was loaded indirectly, it only has environment globals
        base_tmpl = env.get_template("base")
        if tag == "include":
            assert base_tmpl.render() == "env"

        # setting template globals on base directly updates the cache;
        # later renderings of base itself use the merged values
        env.get_template("base", globals={"value": "two"})
        if tag == "include":
            assert base_tmpl.render() == "two"
        assert env.get_template("base") is base_tmpl

        # updating the cached caller's globals updates the template in
        # place, without rebuilding it or touching env globals
        assert env.get_template("main", globals={"value": "three"}) is tmpl
        assert env.get_template("main") is tmpl
        if tag == "include":
            # includes re-render with the merged template globals
            assert tmpl.render() == "three"
        assert env.globals.get("value") == ("env" if tag == "include" else None)

    def test_cached_template_globals_extends(self):
        env = Environment(
            loader=loaders.DictLoader(
                {"base": "{{ value }}", "child": "{% extends 'base' %}"}
            )
        )
        env.globals["value"] = "env"

        child = env.get_template("child", globals={"value": "one"})
        assert child.render() == "one"

        base = env.get_template("base")
        assert base.render() == "env"
        env.get_template("base", globals={"value": "two"})
        assert base.render() == "two"

        assert env.get_template("child") is child
        assert child.render() == "one"
        assert env.globals["value"] == "env"

    def test_cached_template_globals_select_template(self):
        env = Environment(loader=loaders.DictLoader({"foo": "{{ value }}"}))
        tmpl = env.select_template(
            ["missing", "foo"], globals={"value": "one"}
        )
        assert tmpl.render() == "one"
        assert (
            env.select_template(["missing", "foo"], globals={"value": "two"})
            is tmpl
        )
        assert tmpl.render() == "two"

    def test_auto_reload_off_updates_cached_globals(self):
        changed = False

        class TestLoader(loaders.BaseLoader):
            def get_source(self, environment, template):
                return "{{ value }}", None, lambda: not changed

        env = Environment(loader=TestLoader(), auto_reload=False)
        tmpl = env.get_template("foo", globals={"value": "one"})
        assert tmpl.render() == "one"
        # even if the source were stale the cache is authoritative
        changed = True
        assert env.get_template("foo", globals={"value": "two"}) is tmpl
        assert tmpl.render() == "two"

    def test_auto_reload_on_updates_globals_without_source_change(self):
        mapping = {"foo": "{{ value }}"}
        env = Environment(
            loader=loaders.DictLoader(mapping), auto_reload=True
        )
        tmpl = env.get_template("foo", globals={"value": "one"})
        assert tmpl.render() == "one"
        # up to date -> cache hit -> globals merged into cached template
        assert env.get_template("foo", globals={"value": "two"}) is tmpl
        assert tmpl.render() == "two"

    def test_auto_reload_on_stale_template_reloads_with_globals(self):
        state = {"source": "{{ value }}", "changed": False}

        class TestLoader(loaders.BaseLoader):
            def get_source(self, environment, template):
                def uptodate():
                    return not state["changed"]

                return state["source"], None, uptodate

        env = Environment(loader=TestLoader(), auto_reload=True)
        tmpl = env.get_template("foo", globals={"value": "one"})
        assert tmpl.render() == "one"
        # source changes: the cache entry is reloaded, new globals apply
        state["source"] = "{{ value }}!"
        state["changed"] = True
        reloaded = env.get_template("foo", globals={"value": "two"})
        assert reloaded is not tmpl
        assert reloaded.render() == "two!"
        assert env.globals.get("value") is None

    def test_split_template_path(self):
        assert split_template_path("foo/bar") == ["foo", "bar"]
        assert split_template_path("./foo/bar") == ["foo", "bar"]
        pytest.raises(TemplateNotFound, split_template_path, "../foo")


class TestFileSystemLoader:
    searchpath = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "res", "templates"
    )

    @staticmethod
    def _test_common(env):
        tmpl = env.get_template("test.html")
        assert tmpl.render().strip() == "BAR"
        tmpl = env.get_template("foo/test.html")
        assert tmpl.render().strip() == "FOO"
        pytest.raises(TemplateNotFound, env.get_template, "missing.html")

    def test_searchpath_as_str(self):
        filesystem_loader = loaders.FileSystemLoader(self.searchpath)

        env = Environment(loader=filesystem_loader)
        self._test_common(env)

    def test_searchpath_as_pathlib(self):
        import pathlib

        searchpath = pathlib.Path(self.searchpath)
        filesystem_loader = loaders.FileSystemLoader(searchpath)
        env = Environment(loader=filesystem_loader)
        self._test_common(env)

    def test_searchpath_as_list_including_pathlib(self):
        import pathlib

        searchpath = pathlib.Path(self.searchpath)
        filesystem_loader = loaders.FileSystemLoader(["/tmp/templates", searchpath])
        env = Environment(loader=filesystem_loader)
        self._test_common(env)

    def test_caches_template_based_on_mtime(self):
        filesystem_loader = loaders.FileSystemLoader(self.searchpath)

        env = Environment(loader=filesystem_loader)
        tmpl1 = env.get_template("test.html")
        tmpl2 = env.get_template("test.html")
        assert tmpl1 is tmpl2

        os.utime(os.path.join(self.searchpath, "test.html"), (time.time(), time.time()))
        tmpl3 = env.get_template("test.html")
        assert tmpl1 is not tmpl3

    @pytest.mark.parametrize(
        ("encoding", "expect"),
        [
            ("utf-8", "文字化け"),
            ("iso-8859-1", "æ\x96\x87\xe5\xad\x97\xe5\x8c\x96\xe3\x81\x91"),
        ],
    )
    def test_uses_specified_encoding(self, encoding, expect):
        loader = loaders.FileSystemLoader(self.searchpath, encoding=encoding)
        e = Environment(loader=loader)
        t = e.get_template("mojibake.txt")
        assert t.render() == expect


class TestModuleLoader:
    archive = None

    def compile_down(self, prefix_loader, zip="deflated"):
        log = []
        self.reg_env = Environment(loader=prefix_loader)
        if zip is not None:
            fd, self.archive = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
        else:
            self.archive = tempfile.mkdtemp()
        self.reg_env.compile_templates(self.archive, zip=zip, log_function=log.append)
        self.mod_env = Environment(loader=loaders.ModuleLoader(self.archive))
        return "".join(log)

    def teardown(self):
        if hasattr(self, "mod_env"):
            if os.path.isfile(self.archive):
                os.remove(self.archive)
            else:
                shutil.rmtree(self.archive)
            self.archive = None

    def test_log(self, prefix_loader):
        log = self.compile_down(prefix_loader)
        assert (
            'Compiled "a/foo/test.html" as '
            "tmpl_a790caf9d669e39ea4d280d597ec891c4ef0404a" in log
        )
        assert "Finished compiling templates" in log
        assert (
            'Could not compile "a/syntaxerror.html": '
            "Encountered unknown tag 'endif'" in log
        )

    def _test_common(self):
        tmpl1 = self.reg_env.get_template("a/test.html")
        tmpl2 = self.mod_env.get_template("a/test.html")
        assert tmpl1.render() == tmpl2.render()

        tmpl1 = self.reg_env.get_template("b/justdict.html")
        tmpl2 = self.mod_env.get_template("b/justdict.html")
        assert tmpl1.render() == tmpl2.render()

    def test_deflated_zip_compile(self, prefix_loader):
        self.compile_down(prefix_loader, zip="deflated")
        self._test_common()

    def test_stored_zip_compile(self, prefix_loader):
        self.compile_down(prefix_loader, zip="stored")
        self._test_common()

    def test_filesystem_compile(self, prefix_loader):
        self.compile_down(prefix_loader, zip=None)
        self._test_common()

    def test_weak_references(self, prefix_loader):
        self.compile_down(prefix_loader)
        self.mod_env.get_template("a/test.html")
        key = loaders.ModuleLoader.get_template_key("a/test.html")
        name = self.mod_env.loader.module.__name__

        assert hasattr(self.mod_env.loader.module, key)
        assert name in sys.modules

        # unset all, ensure the module is gone from sys.modules
        self.mod_env = None

        try:
            import gc

            gc.collect()
        except BaseException:
            pass

        assert name not in sys.modules

    def test_choice_loader(self, prefix_loader):
        self.compile_down(prefix_loader)
        self.mod_env.loader = loaders.ChoiceLoader(
            [self.mod_env.loader, loaders.DictLoader({"DICT_SOURCE": "DICT_TEMPLATE"})]
        )
        tmpl1 = self.mod_env.get_template("a/test.html")
        assert tmpl1.render() == "BAR"
        tmpl2 = self.mod_env.get_template("DICT_SOURCE")
        assert tmpl2.render() == "DICT_TEMPLATE"

    def test_prefix_loader(self, prefix_loader):
        self.compile_down(prefix_loader)
        self.mod_env.loader = loaders.PrefixLoader(
            {
                "MOD": self.mod_env.loader,
                "DICT": loaders.DictLoader({"test.html": "DICT_TEMPLATE"}),
            }
        )
        tmpl1 = self.mod_env.get_template("MOD/a/test.html")
        assert tmpl1.render() == "BAR"
        tmpl2 = self.mod_env.get_template("DICT/test.html")
        assert tmpl2.render() == "DICT_TEMPLATE"

    def test_path_as_pathlib(self, prefix_loader):
        self.compile_down(prefix_loader)

        mod_path = self.mod_env.loader.module.__path__[0]

        import pathlib

        mod_loader = loaders.ModuleLoader(pathlib.Path(mod_path))
        self.mod_env = Environment(loader=mod_loader)

        self._test_common()

    def test_supports_pathlib_in_list_of_paths(self, prefix_loader):
        self.compile_down(prefix_loader)

        mod_path = self.mod_env.loader.module.__path__[0]

        import pathlib

        mod_loader = loaders.ModuleLoader([pathlib.Path(mod_path), "/tmp/templates"])
        self.mod_env = Environment(loader=mod_loader)

        self._test_common()


@pytest.fixture()
def package_dir_loader(monkeypatch):
    monkeypatch.syspath_prepend(os.path.dirname(__file__))
    return PackageLoader("res")


@pytest.mark.parametrize(
    ("template", "expect"), [("foo/test.html", "FOO"), ("test.html", "BAR")]
)
def test_package_dir_source(package_dir_loader, template, expect):
    source, name, up_to_date = package_dir_loader.get_source(None, template)
    assert source.rstrip() == expect
    assert name.endswith(os.path.join(*split_template_path(template)))
    assert up_to_date()


def test_package_dir_list(package_dir_loader):
    templates = package_dir_loader.list_templates()
    assert "foo/test.html" in templates
    assert "test.html" in templates


@pytest.fixture()
def package_zip_loader(monkeypatch):
    monkeypatch.syspath_prepend(
        os.path.join(os.path.dirname(__file__), "res", "package.zip")
    )
    return PackageLoader("t_pack")


@pytest.mark.parametrize(
    ("template", "expect"), [("foo/test.html", "FOO"), ("test.html", "BAR")]
)
def test_package_zip_source(package_zip_loader, template, expect):
    source, name, up_to_date = package_zip_loader.get_source(None, template)
    assert source.rstrip() == expect
    assert name.endswith(os.path.join(*split_template_path(template)))
    assert up_to_date is None


@pytest.mark.xfail(
    platform.python_implementation() == "PyPy",
    reason="PyPy's zipimporter doesn't have a '_files' attribute.",
    raises=TypeError,
)
def test_package_zip_list(package_zip_loader):
    assert package_zip_loader.list_templates() == ["foo/test.html", "test.html"]


def test_pep_451_import_hook():
    class ImportHook(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path=None, target=None):
            if name != "res":
                return None

            spec = importlib.machinery.PathFinder.find_spec(name)
            return importlib.util.spec_from_file_location(
                name,
                spec.origin,
                loader=self,
                submodule_search_locations=spec.submodule_search_locations,
            )

        def create_module(self, spec):
            return None  # default behaviour is fine

        def exec_module(self, module):
            return None  # we need this to satisfy the interface, it's wrong

    # ensure we restore `sys.meta_path` after putting in our loader
    before = sys.meta_path[:]

    try:
        sys.meta_path.insert(0, ImportHook())
        package_loader = PackageLoader("res")
        assert "test.html" in package_loader.list_templates()
    finally:
        sys.meta_path[:] = before
