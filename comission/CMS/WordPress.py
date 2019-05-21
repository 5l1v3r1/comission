#!/usr/bin/env python3

import io
import os
import re
import zipfile
from distutils.version import LooseVersion
from filecmp import dircmp
from typing import List, Tuple, Union, Dict, Pattern

import requests
from checksumdir import dirhash

import comission.utilsCMS as uCMS
from comission.utilsCMS import Log as log
from .GenericCMS import GenericCMS


class WP(GenericCMS):
    """ WordPress object """

    site_url = "https://wordpress.org/"
    release_site = "https://api.wordpress.org/core/version-check/1.7/"
    download_core_url = "https://wordpress.org/wordpress-"
    download_addon_url = "https://downloads.wordpress.org/plugin/"
    cve_ref_url = "https://wpvulndb.com/api/v3/"

    def __init__(self, dir_path, wp_content, plugins_dir, themes_dir, wpvulndb_token):
        super().__init__()
        self.dir_path = dir_path
        self.wp_content = wp_content
        self.plugins_dir = plugins_dir
        self.themes_dir = themes_dir
        self.wpvulndb_token = wpvulndb_token

        self.regex_version_core = re.compile("\$wp_version = '(.*)';")
        self.regex_version_addon = re.compile("(?i)Version: (.*)")
        self.regex_version_addon_web_plugin = re.compile('"softwareVersion": "(.*)"')
        self.regex_version_addon_web_theme = re.compile("Version: <strong>(.*)</strong>")
        self.regex_date_last_release_plugin = re.compile('"dateModified": "(.*)"')
        self.regex_date_last_release_theme = re.compile("Last updated: <strong>(.*)</strong>")

        self.ignored_files = [
            ".git",
            "cache",
            "plugins",
            "themes",
            "images",
            "license.txt",
            "readme.html",
            "version.php",
        ]

        self.version_files_selector = {"wp-includes/version.php": self.regex_version_core}

        if self.wp_content == "":
            # Take the first directory. Force it with --wp-content if you want another one.
            self.wp_content = self.get_wp_content(dir_path)[0]

        # If no custom plugins directory, then it's in wp-content
        if self.plugins_dir == "":
            self.plugins_dir = os.path.join(self.dir_path, self.wp_content, "plugins")

        # If no custom themes directory, then it's in wp-content
        if self.themes_dir == "":
            self.themes_dir = os.path.join(self.dir_path, self.wp_content, "themes")

    def get_wp_content(self, dir_path: str) -> List[str]:
        tocheck = {"plugins", "themes"}
        suspects = []
        for dirname in next(os.walk(dir_path))[1]:
            if tocheck.issubset(next(os.walk(os.path.join(dir_path, dirname)))[1]):
                suspects.append(dirname)
        if len(suspects) > 1:
            log.print_cms(
                "warning",
                "[+] Several directories are suspected to be wp-contents. "
                "Please check and if needed force one with --wp-content.",
                "",
                0,
            )
            for path in suspects:
                log.print_cms("info", "[+] " + path, "", 1)
            # If none where found, fallback to default one
        if len(suspects) == 0:
            suspects.append("wp-content")
        return suspects

    def get_addon_main_file(self, addon: Dict, addon_path: str) -> List[str]:
        if addon["type"] == "themes":
            addon["filename"] = "style.css"

        elif addon["type"] == "plugins":
            main_file = []

            filename_list = [addon["name"] + ".php", "plugin.php"]

            if addon.get("mu") != "YES":
                filename_list.append("plugin.php")

            for filename in filename_list:
                if os.path.isfile(os.path.join(addon_path, filename)):
                    main_file.append(filename)
            if main_file:
                # If the two files exist, the one named as the plugin is more
                # likely to be the main one
                addon["filename"] = main_file[0]
            else:
                # If no file found, put a random name to trigger an error later
                addon["filename"] = "nofile"

        return addon["filename"]

    def get_url_release(self) -> str:
        return self.release_site

    def extract_core_last_version(self, response) -> str:
        page_json = response.json()
        last_version_core = page_json["offers"][0]["version"]
        log.print_cms("info", "[+] Last CMS version: " + last_version_core, "", 0)
        self.core_details["infos"]["last_version"] = last_version_core

        return last_version_core

    def get_addon_last_version(
        self, addon: Dict
    ) -> Tuple[str, Union[None, requests.exceptions.HTTPError]]:
        releases_url = "{}{}/{}/".format(self.site_url, addon["type"], addon["name"])

        if addon["type"] == "plugins":
            version_web_regexp = self.regex_version_addon_web_plugin
            date_last_release_regexp = self.regex_date_last_release_plugin
        elif addon["type"] == "themes":
            version_web_regexp = self.regex_version_addon_web_theme
            date_last_release_regexp = self.regex_date_last_release_theme

        try:
            response = requests.get(releases_url, allow_redirects=False)
            response.raise_for_status()

            if response.status_code == 200:
                page = response.text

                last_version_result = version_web_regexp.search(page)
                date_last_release_result = date_last_release_regexp.search(page)

                if last_version_result and date_last_release_result:
                    addon["last_version"] = last_version_result.group(1)
                    addon["last_release_date"] = date_last_release_result.group(1).split("T")[0]
                    addon["link"] = releases_url

                    if addon["last_version"] == addon["version"]:
                        log.print_cms("good", "Up to date !", "", 1)
                    else:
                        log.print_cms(
                            "alert",
                            "Outdated, last version: ",
                            addon["last_version"]
                            + " ( "
                            + addon["last_release_date"]
                            + " )\n\tCheck : "
                            + releases_url,
                            1,
                        )

        except requests.exceptions.HTTPError as e:
            msg = "Addon not on official site. Search manually !"
            log.print_cms("alert", "[-] " + msg, "", 1)
            addon["notes"] = msg
            return "", e
        return addon["last_version"], None

    def check_addon_alteration(
        self, addon: Dict, addon_path: str, temp_directory: str
    ) -> Tuple[str, Union[None, requests.exceptions.HTTPError]]:

        if addon["version"] == "trunk":
            addon_url = "{}{}.zip".format(self.download_addon_url, addon["name"])
        else:
            addon_url = "{}{}.{}.zip".format(
                self.download_addon_url, addon["name"], addon["version"]
            )

        log.print_cms("default", "To download the addon: " + addon_url, "", 1)
        altered = ""

        try:
            response = requests.get(addon_url)
            response.raise_for_status()

            if response.status_code == 200:
                zip_file = zipfile.ZipFile(io.BytesIO(response.content), "r")
                zip_file.extractall(temp_directory)
                zip_file.close()

                project_dir_hash = dirhash(addon_path, "sha1")
                ref_dir = os.path.join(temp_directory, addon["name"])
                ref_dir_hash = dirhash(ref_dir, "sha1")

                if project_dir_hash == ref_dir_hash:
                    altered = "NO"
                    log.print_cms("good", "Different from sources : " + altered, "", 1)
                else:
                    altered = "YES"
                    log.print_cms("alert", "Different from sources : " + altered, "", 1)

                    ignored = ["css", "img", "js", "fonts", "images"]

                    dcmp = dircmp(addon_path, ref_dir, ignored)
                    uCMS.diff_files(dcmp, addon["alterations"], addon_path)

                addon["altered"] = altered

                if addon["alterations"] is not None:
                    msg = "[+] For further analysis, archive downloaded here : " + ref_dir
                    log.print_cms("info", msg, "", 1)

        except requests.exceptions.HTTPError as e:
            msg = "The download link is not standard. Search manually !"
            log.print_cms("alert", msg, "", 1)
            addon["notes"] = msg
            return msg, e

        return altered, None

    def check_vulns_core(
        self, version_core: str
    ) -> Tuple[Union[str, List], Union[None, requests.exceptions.HTTPError]]:
        vulns_details = []
        version = version_core.replace(".", "")
        url = "{}wordpresses/{}".format(self.cve_ref_url, version)
        url_details = "https://wpvulndb.com/vulnerabilities/"
        token_header = "Token token={}".format(self.wpvulndb_token)
        headers = {"Authorization": token_header}

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            if response.status_code == 200:
                page_json = response.json()

                vulns = page_json[version_core]["vulnerabilities"]
                log.print_cms("info", "[+] CVE list", "", 1)

                if len(vulns) > 0:
                    for vuln in vulns:

                        vuln_details = {
                            "name": "",
                            "link": "",
                            "type": "",
                            "poc": "",
                            "fixed_in": "",
                        }

                        vuln_url = url_details + str(vuln["id"])

                        vuln_details["name"] = vuln["title"]
                        vuln_details["link"] = vuln_url
                        vuln_details["type"] = vuln["vuln_type"]
                        vuln_details["poc"] = "CHECK"
                        vuln_details["fixed_in"] = vuln["fixed_in"]

                        if uCMS.get_poc(vuln_url):
                            vuln_details["poc"] = "YES"

                        log.print_cms("alert", vuln["title"], "", 1)
                        log.print_cms(
                            "info", "[+] Fixed in version " + str(vuln["fixed_in"]), "", 1
                        )

                        vulns_details.append(vuln_details)
                else:
                    log.print_cms("good", "No CVE were found", "", 1)

        except requests.exceptions.HTTPError as e:
            log.print_cms("info", "No entry on wpvulndb.", "", 1)
            return "", e
        return vulns_details, None

    def check_vulns_addon(
        self, addon: Dict
    ) -> Tuple[Union[str, List[Dict]], Union[None, requests.exceptions.HTTPError]]:
        vulns = []
        url_details = "https://wpvulndb.com/vulnerabilities/"
        token_header = "Token token={}".format(self.wpvulndb_token)
        headers = {"Authorization": token_header}

        try:
            url = "{}plugins/{}".format(self.cve_ref_url, addon["name"])

            response = requests.get(url, headers=headers)
            response.raise_for_status()

            if response.status_code == 200:
                page_json = response.json()

                vulns = page_json[addon["name"]]["vulnerabilities"]
                log.print_cms("info", "[+] CVE list", "", 1)

                for vuln in vulns:

                    vuln_url = url_details + str(vuln["id"])
                    vuln_details = {"name": "", "link": "", "type": "", "poc": "", "fixed_in": ""}

                    try:
                        if LooseVersion(addon["version"]) < LooseVersion(vuln["fixed_in"]):
                            log.print_cms("alert", vuln["title"], "", 1)

                            vuln_details["name"] = vuln["title"]
                            vuln_details["link"] = vuln_url
                            vuln_details["type"] = vuln["vuln_type"]
                            vuln_details["fixed_in"] = vuln["fixed_in"]
                            vuln_details["poc"] = "CHECK"

                            if uCMS.get_poc(vuln_url):
                                vuln_details["poc"] = "YES"

                            addon["vulns"].append(vuln_details)

                    except (TypeError, AttributeError):
                        log.print_cms(
                            "alert",
                            "Unable to compare version. Please check this "
                            "vulnerability :" + vuln["title"],
                            "",
                            1,
                        )

                        vuln_details["name"] = " To check : " + vuln["title"]
                        vuln_details["link"] = vuln_url
                        vuln_details["type"] = vuln["vuln_type"]
                        vuln_details["fixed_in"] = vuln["fixed_in"]
                        vuln_details["poc"] = "CHECK"

                        if uCMS.get_poc(vuln_url):
                            vuln_details["poc"] = "YES"

                        addon["vulns"].append(vuln_details)

                if addon["vulns"]:
                    addon["cve"] = "YES"
                else:
                    addon["cve"] = "NO"

        except requests.exceptions.HTTPError as e:
            msg = "No entry on wpvulndb."
            log.print_cms("info", "[+] " + msg, "", 1)
            addon["cve"] = "NO"
            return "", e
        return vulns, None

    def get_archive_name(self):
        return "wordpress"

    def addon_analysis(self, addon_type: str) -> List[Dict]:
        temp_directory = uCMS.TempDir.create()
        addons = []

        log.print_cms(
            "info",
            "\n#######################################################"
            + "\n\t\t"
            + addon_type
            + " analysis"
            + "\n#######################################################",
            "",
            0,
        )

        addons_paths = {}

        if addon_type == "plugins":
            addons_paths = {
                "standard": self.plugins_dir,
                "mu": os.path.join(self.dir_path, self.wp_content, "mu-plugins"),
            }
        elif addon_type == "themes":
            addons_paths = {"standard": self.themes_dir}

        for key, addons_path in addons_paths.items():
            # Get the list of addon to work with
            addons_name = uCMS.fetch_addons(addons_path, key)

            for addon_name in addons_name:
                addon = {
                    "type": addon_type,
                    "status": "todo",
                    "name": addon_name,
                    "version": "",
                    "last_version": "Not found",
                    "last_release_date": "",
                    "link": "",
                    "altered": "",
                    "cve": "",
                    "vulns": [],
                    "notes": "",
                    "alterations": [],
                    "filename": "",
                    "path": "",
                }
                log.print_cms("info", "[+] " + addon_name, "", 0)

                addon_path = os.path.join(addons_path, addon_name)

                if addon_type == "plugins":
                    if key == "mu":
                        addon["mu"] = "YES"
                        addon_path = os.path.join(addons_path)
                    else:
                        addon["mu"] = "NO"

                # Check addon main file
                self.get_addon_main_file(addon, addon_path)

                # Get addon version
                _, err = self.get_addon_version(addon, addon_path, self.regex_version_addon, " ")
                if err is not None:
                    addons.append(addon)
                    continue

                # Check addon last version
                _, err = self.get_addon_last_version(addon)
                if err is not None:
                    addons.append(addon)
                    continue

                # Check known CVE in wpvulndb
                _, err = self.check_vulns_addon(addon)
                if err is not None:
                    addons.append(addon)
                    continue

                # Check if the addon have been altered
                _, err = self.check_addon_alteration(addon, addon_path, temp_directory)
                if err is not None:
                    addons.append(addon)
                    continue

                addons.append(addon)

        if addon_type == "plugins":
            self.plugins = addons
        elif addon_type == "themes":
            self.themes = addons

        return addons
