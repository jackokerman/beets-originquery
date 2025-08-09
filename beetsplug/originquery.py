import glob
import json
import os
import re
from collections import OrderedDict
from pathlib import Path

import confuse
import jsonpath_rw
import yaml
from beets import config, ui
from beets.autotag import current_metadata
from beets.plugins import BeetsPlugin

BEETS_TO_LABEL = OrderedDict(
    [
        ("media", "Media"),
        ("year", "Edition year"),
        ("country", "Country"),
        ("label", "Record label"),
        ("catalognum", "Catalog number"),
        ("albumdisambig", "Edition"),
    ]
)

# Conflicts will be reported if any of these fields don't match.
CONFLICT_FIELDS = ["catalognum", "media"]

# Supported metadata sources that can provide extra tags
SUPPORTED_METADATA_SOURCES = ["musicbrainz", "discogs"]


def escape_braces(string):
    return string.replace("{", "{{").replace("}", "}}")


def normalize_catno(catno):
    return catno.upper().replace(" ", "").replace("-", "")


def sanitize_value(key, value):
    if key == "media" and value == "WEB":
        return "Digital Media"
    if key == "catalognum" or key == "label":
        return re.split("[,/]", value)[0].strip()
    if key == "year" and value == "0":
        return ""
    return value


def highlight(text, active=True):
    if active:
        return ui.colorize("text_highlight_minor", text)
    return text


class OriginQuery(BeetsPlugin):
    def __init__(self):
        super().__init__()

        def fail(msg):
            self.error(msg)
            self.error("Plugin disabled.")

        # Use the first available source's extra tags
        self.extra_tags = []
        self.extra_tags_source = None

        for source in SUPPORTED_METADATA_SOURCES:
            try:
                source_extra_tags = config[source]["extra_tags"].get()
                if source_extra_tags and len(source_extra_tags):
                    self.extra_tags = source_extra_tags
                    self.extra_tags_source = source
                    break
            except confuse.NotFoundError:
                # This source doesn't have extra_tags configured, skip it
                continue

        if not self.extra_tags:
            return fail(
                f"Config error: No extra tags found from supported metadata sources "
                f"({', '.join(SUPPORTED_METADATA_SOURCES)}). "
                f"At least one source must have extra_tags configured."
            )

        self.info(f"Using extra tags from: {self.extra_tags_source}")
        self.info(f"Available extra tags: {', '.join(self.extra_tags)}")

        config_patterns = None
        try:
            config_patterns = self.config["tag_patterns"].get()
            if not isinstance(config_patterns, dict):
                raise confuse.ConfigError()
        except confuse.ConfigError:
            return fail(
                "Config error: originquery.tag_patterns must be set to a "
                "dictionary of key -> pattern mappings."
            )

        try:
            self.origin_file = Path(self.config["origin_file"].get())
        except confuse.NotFoundError:
            return fail("Config error: originquery.origin_file not set.")
        self.tag_patterns = {}

        try:
            origin_type = (
                self.config["origin_type"].as_choice(["yaml", "json", "text"]).lower()
            )
        except confuse.NotFoundError:
            origin_type = self.origin_file.suffix.lower()[1:]

        if origin_type == "json":
            self.match_fn = self.match_json
        elif origin_type == "yaml":
            self.match_fn = self.match_yaml
        else:
            self.match_fn = self.match_text

        for key, pattern in config_patterns.items():
            if key not in BEETS_TO_LABEL:
                return fail(f'Config error: unknown key "{key}"')
                self.error("Plugin disabled.")

            if origin_type == "json" or origin_type == "yaml":
                try:
                    self.tag_patterns[key] = jsonpath_rw.parse(pattern)
                except Exception as e:
                    return fail(
                        f'Config error: invalid tag pattern for "{key}". '
                        f'"{pattern}" is not a valid JSON path ({format(str(e))}).'
                    )
                continue

            try:
                regex = re.compile(pattern)
                self.tag_patterns[key] = regex
            except re.error as e:
                return fail(
                    f'Config error: invalid tag pattern for "{key}". '
                    f'"{pattern}" is not a valid regex ({format(str(e))}).'
                )
            if regex.groups != 1:
                return fail(
                    f'Config error: invalid tag pattern for "{key}". '
                    f'"{pattern}" must have exactly one capture group.'
                )

        self.register_listener("import_task_start", self.import_task_start)
        self.register_listener("before_choose_candidate", self.before_choose_candidate)
        self.tasks = {}

        try:
            self.use_origin_on_conflict = self.config["use_origin_on_conflict"].get(
                bool
            )
        except confuse.NotFoundError:
            self.use_origin_on_conflict = False

        try:
            self.preserve_media_with_catalognum = self.config[
                "preserve_media_with_catalognum"
            ].get(bool)
        except confuse.NotFoundError:
            self.preserve_media_with_catalognum = False

    def error(self, msg):
        self._log.error(escape_braces(ui.colorize("text_error", msg)))

    def warn(self, msg):
        self._log.warning(escape_braces(ui.colorize("text_warning", msg)))

    def info(self, msg):
        # beets defaults to log level warning for event handlers.
        self._log.warning(escape_braces(msg))

    def print_tags(self, items, use_tagged):
        headers = ["Field", "Tagged Data", "Origin Data"]

        w_key = max(len(headers[0]), *(len(BEETS_TO_LABEL[k]) for k, v in items))
        w_tagged = max(len(headers[1]), *(len(v["tagged"]) for k, v in items))
        w_origin = max(len(headers[2]), *(len(v["origin"]) for k, v in items))

        self.info(
            f"╔{'═' * (w_key + 2)}╤{'═' * (w_tagged + 2)}╤{'═' * (w_origin + 2)}╗"
        )
        self.info(
            f"║ {headers[0].ljust(w_key)} │ "
            f"{highlight(headers[1].ljust(w_tagged), use_tagged)} │ "
            f"{highlight(headers[2].ljust(w_origin), not use_tagged)} ║"
        )
        self.info(
            f"╟{'─' * (w_key + 2)}┼{'─' * (w_tagged + 2)}┼{'─' * (w_origin + 2)}╢"
        )
        for k, v in items:
            if not v["tagged"] and not v["origin"]:
                continue
            tagged_active = use_tagged and v["active"]
            origin_active = not use_tagged and v["active"]
            self.info(
                f"║ {BEETS_TO_LABEL[k].ljust(w_key)} │ "
                f"{highlight(v['tagged'].ljust(w_tagged), tagged_active)} │ "
                f"{highlight(v['origin'].ljust(w_origin), origin_active)} ║"
            )
        self.info(
            f"╚{'═' * (w_key + 2)}╧{'═' * (w_tagged + 2)}╧{'═' * (w_origin + 2)}╝"
        )

    def before_choose_candidate(self, task, session):
        task_info = self.tasks[task]
        origin_path = task_info["origin_path"]

        if task_info.get("missing_origin", False):
            self.warn(f"No origin file found at {origin_path}")
            return
        else:
            self.info(f"Using origin file {origin_path}")

        conflict = task_info.get("conflict", False)
        use_tagged = conflict and not self.use_origin_on_conflict
        self.print_tags(task_info.get("tag_compare").items(), use_tagged)

        if conflict:
            self.warn("Origin data conflicts with tagged data.")

    def match_text(self, origin_path):
        with open(origin_path, encoding="utf-8") as f:
            lines = f.readlines()

        for key, pattern in self.tag_patterns.items():
            for line in lines:
                line = line.strip()
                match = re.match(pattern, line)
                if not match:
                    continue
                yield key, match[1]

    def match_json(self, origin_path):
        with open(origin_path, encoding="utf-8") as f:
            data = json.load(f)

        for key, pattern in self.tag_patterns.items():
            match = pattern.find(data)
            if not len(match):
                continue

            yield key, str(match[0].value)

    def match_yaml(self, origin_path):
        with open(origin_path, encoding="utf-8") as f:
            data = yaml.load(f, Loader=yaml.SafeLoader)

        for key, pattern in self.tag_patterns.items():
            match = pattern.find(data)
            if not len(match) or not match[0].value:
                continue
            yield key, str(match[0].value)

    def import_task_start(self, task, session):
        task_info = self.tasks[task] = {}

        # In case this is a multi-disc import, find the common parent directory.
        base = os.path.commonpath(task.paths).decode("utf8")

        glob_pattern = os.path.join(glob.escape(base), self.origin_file)
        origin_glob = sorted(glob.glob(glob_pattern))
        if len(origin_glob) < 1:
            task_info["origin_path"] = Path(base) / self.origin_file
            task_info["missing_origin"] = True
            return
        task_info["origin_path"] = origin_path = Path(origin_glob[0])

        conflict = False
        likelies, consensus = current_metadata(task.items)
        task_info["tag_compare"] = tag_compare = OrderedDict()
        for tag in BEETS_TO_LABEL:
            tag_compare.update(
                {
                    tag: {
                        "tagged": str(likelies[tag]),
                        "active": tag in self.extra_tags,
                        "origin": "",
                    }
                }
            )

        for key, value in self.match_fn(origin_path):
            if tag_compare[key]["origin"]:
                continue

            tagged_value = tag_compare[key]["tagged"]
            origin_value = sanitize_value(key, value)
            tag_compare[key]["origin"] = origin_value
            if key not in CONFLICT_FIELDS or not tagged_value or not origin_value:
                continue

            if key == "catalognum":
                tagged_value = normalize_catno(tagged_value)
                origin_value = normalize_catno(origin_value)

            if tagged_value != origin_value:
                conflict = task_info["conflict"] = True

        if not conflict or self.use_origin_on_conflict:
            # Update all item with origin metadata.
            for item in task.items:
                for tag, entry in tag_compare.items():
                    origin_value = entry["origin"]
                    if tag not in self.extra_tags:
                        continue
                    if tag == "year" and origin_value:
                        origin_value = (
                            int(origin_value) if origin_value.isdigit() else ""
                        )
                    item[tag] = origin_value

                # Apply the media removal workaround by default
                # beets weighs media heavily, and will even prioritize a media match
                # over an exact catalognum match. At the same time, media for uploaded
                # music is often mislabeled (e.g., Enhanced CD and SACD are just
                # grouped as CD). This does not make a good combination. As a
                # workaround, remove the media from the item if we also have a
                # catalognum, unless the config option is set to preserve it.
                if (
                    not self.preserve_media_with_catalognum
                    and item.get("media")
                    and item.get("catalognum")
                ):
                    del item["media"]
                    tag_compare["media"]["active"] = False
