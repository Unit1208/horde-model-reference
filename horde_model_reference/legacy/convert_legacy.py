from abc import ABC, abstractmethod
import typing
from pydantic import ValidationError
from horde_model_reference import consts
from horde_model_reference.consts import MODEL_REFERENCE_GITHUB_REPO, LEGACY_REFERENCE_FOLDER
from horde_model_reference.util import model_name_to_showcase_folder_name
from horde_model_reference.legacy.legacy_model_database_records import (
    Legacy_StableDiffusion_ModelRecord,
    Legacy_Config_DownloadRecord,
    Legacy_Config_FileRecord,
    Legacy_StableDiffusion_ModelReference,
    Legacy_Generic_ModelRecord,
)

from horde_model_reference.model_database_records import StableDiffusionModelReference
from pathlib import Path
import json
import glob
import urllib.parse


class LegacyConverterBase(ABC):
    legacy_folder_path: Path
    """The folder path to the legacy model reference."""
    legacy_database_path: Path
    """The file path to the legacy stable diffusion model reference database."""
    converted_folder_path: Path
    """The folder path to write write any converted."""
    converted_database_file_path: Path
    """The file path to write the converted stable diffusion model reference database."""

    all_model_records: dict[str, Legacy_Generic_ModelRecord] = {}
    """All the models entries in found that will be converted."""

    all_validation_errors_log: dict[str, list[str]] = {}

    debug_mode: bool = False
    print_errors: bool = True

    def add_validation_error_to_log(self, *, model_record_key: str, error: str) -> None:
        if model_record_key not in self.all_validation_errors_log:
            self.all_validation_errors_log[model_record_key] = []
        self.all_validation_errors_log[model_record_key].append(error)
        if self.print_errors:
            print("-> " + error)

    def __init__(
        self,
        *,
        legacy_folder_path: str | Path = LEGACY_REFERENCE_FOLDER,
        target_file_folder: str | Path,
        model_reference_type: consts.MODEL_REFERENCE_TYPE,
        debug_mode: bool = False,
        print_errors: bool = True,
    ):
        self.legacy_database_filename = consts.get_model_reference_filename(
            model_reference_type=model_reference_type,
        )
        self.legacy_folder_path = Path(legacy_folder_path)
        self.legacy_database_path = consts.get_model_reference_filename(
            model_reference_type=model_reference_type,
            basePath=legacy_folder_path,
        )
        self.converted_folder_path = Path(target_file_folder)
        self.converted_database_file_path = self.converted_folder_path.joinpath(self.legacy_database_filename)

        self.debug_mode = debug_mode
        self.print_errors = print_errors

    @abstractmethod
    def normalize_and_convert(self) -> bool:
        """Normalizes and converts the legacy model reference database to the new format.

        Returns:
            bool: True if the conversion was successful, False otherwise.
        """

    def _iterate_over_input_records(
        self, modelrecord_type: type[Legacy_Generic_ModelRecord]
    ) -> typing.Iterator[tuple[str, Legacy_Generic_ModelRecord]]:
        raw_legacy_json_data: dict = {}
        """Return an iterator over the legacy model reference database.

        Yields:
            Iterator[tuple[str, Legacy_Generic_ModelRecord]]: The model record key and the model record.
        """

        with open(self.legacy_database_path) as legacy_model_reference_file:
            raw_legacy_json_data = json.load(legacy_model_reference_file)

        for model_record_key, model_record_contents in raw_legacy_json_data.items():
            new_record_config_files_list: list[Legacy_Config_FileRecord] = []
            new_record_config_download_list: list[Legacy_Config_DownloadRecord] = []
            try:
                if len(model_record_contents["config"]) > 2:
                    error = f"{model_record_key} has more than 2 config entries."
                    self.add_validation_error_to_log(model_record_key=model_record_key, error=error)
                sha_lookup = {}
                for config_entry in model_record_contents["config"]:
                    if config_entry == "files":
                        for config_file in model_record_contents["config"][config_entry]:
                            parsed_file_record = Legacy_Config_FileRecord(**config_file)
                            if ".yaml" in parsed_file_record.path:
                                continue
                            sha_lookup[parsed_file_record.path] = parsed_file_record.sha256sum
                            parsed_file_record.sha256sum = None
                            new_record_config_files_list.append(parsed_file_record)
                    elif config_entry == "download":
                        for download in model_record_contents["config"][config_entry]:
                            parsed_download_record = Legacy_Config_DownloadRecord(**download)
                            parsed_download_record.sha256sum = sha_lookup[parsed_download_record.file_name]
                            new_record_config_download_list.append(parsed_download_record)

                model_record_contents["config"] = {
                    # "files": new_record_config_files_list,
                    "download": new_record_config_download_list,
                }
                record_as_conversion_class = modelrecord_type(**model_record_contents)
                self.all_model_records[model_record_key] = record_as_conversion_class
                yield model_record_key, record_as_conversion_class
            except ValidationError as e:
                error = f"CRITICAL: Error parsing {model_record_key}:\n{e}"
                self.add_validation_error_to_log(model_record_key=model_record_key, error=error)
                continue

    def generic_record_sanity_checks(self, *, model_record_key: str, record: Legacy_Generic_ModelRecord) -> None:
        #
        # Non-conformity checks
        #
        if record.name != model_record_key:
            error = f"name mismatch for {model_record_key}."

            self.add_validation_error_to_log(model_record_key=model_record_key, error=error)

        if record.available:
            error = f"{model_record_key} is flagged 'available'."

            self.add_validation_error_to_log(model_record_key=model_record_key, error=error)

        if record.download_all:
            if self.debug_mode:
                error = f"{model_record_key} has download_all set."

                self.add_validation_error_to_log(model_record_key=model_record_key, error=error)

        if record.config is None:
            error = f"{model_record_key} has no config."

            self.add_validation_error_to_log(model_record_key=model_record_key, error=error)

        if record.description is None:
            error = f"{model_record_key} has no description."

            self.add_validation_error_to_log(model_record_key=model_record_key, error=error)

        if record.style == "":
            error = f"{model_record_key} has no style."

            self.add_validation_error_to_log(model_record_key=model_record_key, error=error)


class LegacyStableDiffusionConverter(LegacyConverterBase):

    showcase_glob_pattern: str = "horde_model_reference/showcase/*"
    """The glob pattern used to find all showcase folders. Defaults to `'horde_model_reference/showcase/*'`."""
    # todo: extract to consts

    """The name of the legacy model reference database file. Defaults to `'stable_diffusion.json'`."""
    default_showcase_folder_name = "showcase"
    """The expected name of the folder containing all model showcase folders. Defaults to `'showcase'`."""
    # todo: extract to consts

    def __init__(
        self,
        *,
        legacy_folder_path: str | Path = LEGACY_REFERENCE_FOLDER,
        target_file_folder: str | Path,
        debug_mode: bool = False,
        print_errors: bool = True,
    ):
        super().__init__(
            legacy_folder_path=legacy_folder_path,
            target_file_folder=target_file_folder,
            model_reference_type=consts.MODEL_REFERENCE_TYPE.STABLE_DIFFUSION,
            debug_mode=debug_mode,
            print_errors=print_errors,
        )

    def normalize_and_convert(self) -> bool:

        all_baseline_types: dict[str, int] = {}
        """A dictionary of all the baseline types found and the number of times they appear."""
        all_styles: dict[str, int] = {}
        """A dictionary of all the styles found and the number of times they appear."""
        all_tags: dict[str, int] = {}
        """A dictionary of all the tags found and the number of times they appear."""
        all_model_hosts: dict[str, int] = {}
        """A dictionary of all the model hosts found and the number of times they appear."""

        existing_showcase_folders = glob.glob(self.showcase_glob_pattern, recursive=True)
        existing_showcase_files: dict[str, list[str]] = self.get_existing_showcases(existing_showcase_folders)
        """A dictionary of whose keys are the showcase folders and the values are a list of files within."""
        all_model_iterator = self._iterate_over_input_records(Legacy_StableDiffusion_ModelRecord)

        for model_record_key, model_record_in_progress in all_model_iterator:

            if model_record_in_progress is None:
                raise ValueError(f"new_record is None! model_record_key = {model_record_key}")

            if not isinstance(model_record_in_progress, Legacy_StableDiffusion_ModelRecord):
                raise ValueError(
                    f"new_record is not a Legacy_StableDiffusion_ModelRecord! model_record_key = {model_record_key}"
                )

            self.generic_record_sanity_checks(
                model_record_key=model_record_key,
                record=model_record_in_progress,
            )

            all_styles[model_record_in_progress.style] = all_styles.get(model_record_in_progress.style, 0) + 1

            if model_record_in_progress.type != "ckpt":
                error = f"{model_record_key} is not a ckpt!"

                self.add_validation_error_to_log(model_record_key=model_record_key, error=error)

            #
            # Increment baseline type counter
            #
            model_record_in_progress.baseline = self.convert_legacy_baseline(model_record_in_progress.baseline)
            all_baseline_types[model_record_in_progress.baseline] = (
                all_baseline_types.get(model_record_in_progress.baseline, 0) + 1
            )

            #
            # Showcase handling and sanity checks
            #
            expected_showcase_foldername = model_name_to_showcase_folder_name(model_record_key)
            self.create_showcase_folder(expected_showcase_foldername)

            if model_record_in_progress.showcases is not None and len(model_record_in_progress.showcases) > 0:
                if any("huggingface" in showcase for showcase in model_record_in_progress.showcases):
                    error = f"{model_record_key} has a huggingface showcase."

                    self.add_validation_error_to_log(model_record_key=model_record_key, error=error)

                if expected_showcase_foldername not in existing_showcase_files:
                    error = f"{model_record_key} has no showcase folder. Expected: {expected_showcase_foldername}"

                    self.add_validation_error_to_log(model_record_key=model_record_key, error=error)

                model_record_in_progress.showcases = []
                for file in existing_showcase_files[expected_showcase_foldername]:
                    url_friendly_name = urllib.parse.quote(Path(file).name)
                    # if not any(url_friendly_name in showcase for showcase in new_record.showcases):
                    #     print(f"{model_record_key} is missing a showcase for {url_friendly_name}.")
                    #     print(f"{new_record.showcases=}")
                    #     continue
                    expected_github_location = urllib.parse.urljoin(
                        MODEL_REFERENCE_GITHUB_REPO,
                        f"{self.default_showcase_folder_name}/{expected_showcase_foldername}/{url_friendly_name}",
                    )
                    model_record_in_progress.showcases.append(expected_github_location)
            #
            # Increment tag counter
            #
            if model_record_in_progress.tags is not None:
                for tag in model_record_in_progress.tags:
                    all_tags[tag] = all_tags.get(tag, 0) + 1

            #
            # Config handling and sanity checks
            #
            if len(model_record_in_progress.config) == 0:
                error = f"{model_record_key} has no config."
                self.add_validation_error_to_log(model_record_key=model_record_key, error=error)

            config_entries = model_record_in_progress.config
            found_hosts = self.normalize_and_convert_config_entries(
                model_record_key=model_record_key,
                config_entries=config_entries,
            )

            #
            # Increment host counter
            #
            for found_host in found_hosts:
                all_model_hosts[found_host] = all_model_hosts.get(found_host, 0) + 1

        final_on_disk_showcase_folders = glob.glob(self.showcase_glob_pattern, recursive=True)

        for folder in final_on_disk_showcase_folders:
            parsed_folder = Path(folder)

            if parsed_folder.is_file():
                continue

            if not any(parsed_folder.iterdir()):
                error = f"showcase folder '{parsed_folder.name}' is empty."
                self.add_validation_error_to_log(model_record_key=parsed_folder.name, error=error)

        final_on_disk_showcase_folders_names = [
            Path(folder).name for folder in final_on_disk_showcase_folders if Path(folder).is_dir()
        ]
        final_expected_showcase_folders = [
            model_name_to_showcase_folder_name(model_name) for model_name in self.all_model_records
        ]

        for folder in final_on_disk_showcase_folders_names:
            if folder not in final_expected_showcase_folders:
                error = f"folder '{folder}' is not in the model records."
                self.add_validation_error_to_log(model_record_key=folder, error=error)

        print()
        print(f"{all_styles=}")
        print(f"{all_baseline_types=}")
        print(f"{all_tags=}")
        print(f"{all_model_hosts=}")

        print()
        print(f"Total number of models: {len(self.all_model_records)}")
        print(f"Total number of showcase folders: {len(final_on_disk_showcase_folders_names)}")

        print()
        print(f"Total number of models with errors: {len(self.all_validation_errors_log)}")
        print()
        print("Errors and warnings are listed above on lines prefixed with `-> `")

        modelReference = Legacy_StableDiffusion_ModelReference(
            baseline_types=all_baseline_types,
            styles=all_styles,
            tags=all_tags,
            model_hosts=all_model_hosts,
            models={
                key: value
                for key, value in self.all_model_records.items()
                if isinstance(value, Legacy_StableDiffusion_ModelRecord)
            },  # quiets mypy about the potential for upcasting
        )
        jsonToWrite = modelReference.json(
            indent=4,
            exclude_defaults=True,
            exclude_none=True,
            exclude_unset=True,
        )

        try:
            # If this fails, we have a problem. By definition, the model reference should be converted by this point
            # and ready to be cast to the new model reference type.
            StableDiffusionModelReference(**json.loads(jsonToWrite))
        except ValidationError as e:
            print(e)
            print("CRITICAL: Failed to convert to new model reference type.")
            raise e

        with open(self.converted_database_file_path, "w") as testfile:
            testfile.write(jsonToWrite)

        print("Converted database passes validation and was written to disk successfully.")
        print(f"Converted database written to: {self.converted_database_file_path}")
        return True

    def get_existing_showcases(self, existing_showcase_folders: list[str]) -> dict[str, list[str]]:
        """Get a dictionary of all existing showcase folders and their contents."""
        existing_showcase_files: dict[str, list[str]] = {}
        for showcase_folder in existing_showcase_folders:
            model_showcase_files = glob.glob(str(Path(showcase_folder).joinpath("*")), recursive=True)
            model_showcase_folder_name = model_name_to_showcase_folder_name(Path(showcase_folder).name)

            existing_showcase_files[model_showcase_folder_name] = model_showcase_files

        return existing_showcase_files

    def convert_legacy_baseline(self, baseline: str):
        if baseline == "stable diffusion 1":
            baseline = "stable_diffusion_1"
            # new_record.baseline_trained_resolution = 256
        elif baseline == "stable diffusion 2":
            baseline = "stable_diffusion_2_768"
        elif baseline == "stable diffusion 2 512":
            baseline = "stable_diffusion_2_512"
        return baseline

    def create_showcase_folder(self, expected_showcase_foldername: str):
        newFolder = self.converted_folder_path.joinpath(self.default_showcase_folder_name)
        newFolder = newFolder.joinpath(expected_showcase_foldername)
        newFolder.mkdir(parents=True, exist_ok=True)

    def normalize_and_convert_config_entries(
        self,
        *,
        model_record_key: str,
        config_entries: dict[str, list[Legacy_Config_FileRecord | Legacy_Config_DownloadRecord]],
    ) -> dict[str, int]:
        """Normalize and convert a config entries. This changes the contents of param `config_entries`.

        Args:
            model_record_key (str): The key of the model record.
            config_entries (see type hints): The config entries to normalize and convert.

        Raises:
            TypeError: Raised if a config file definition is under the wrong key.

        Returns:
            dict[str, int]: A dict of the hosts and the number of files they host for this model.
        """
        download_hosts: dict[str, int] = {}
        for config_entry_key, config_entry_object in config_entries.items():
            if config_entry_key == "files":
                for config_file in config_entry_object:
                    if not isinstance(config_file, Legacy_Config_FileRecord):
                        print(f"{model_record_key} is in 'files' but isn't a `Legacy_Config_FileRecord`!")
                        raise TypeError("Expected `Legacy_Config_FileRecord`.")
                    if config_file.path is None or config_file.path == "":
                        print(f"{model_record_key} has a config file with no path.")

                    if ".yaml" in config_file.path:
                        if config_file.path != "v2-inference-v.yaml" and config_file.path != "v1-inference.yaml":
                            print(f"{model_record_key} has a non-standard config.")
                        continue
                    elif ".ckpt" not in config_file.path:
                        print(f"{model_record_key} does not have a ckpt file specified.")

                    if config_file.sha256sum is None or config_file.sha256sum == "":
                        print(f"{model_record_key} has a config file with no sha256sum.")
                    else:
                        if len(config_file.sha256sum) != 64:
                            print(f"{model_record_key} has a config file with an invalid sha256sum.")

            elif config_entry_key == "download":
                for download in config_entry_object:
                    if not isinstance(download, Legacy_Config_DownloadRecord):
                        print(f"{model_record_key} is in 'download' but isn't a `Legacy_Config_DownloadRecord`!")
                        raise TypeError("Expected `Legacy_Config_DownloadRecord`.")
                    if download.file_name is None or download.file_name == "":
                        print(f"{model_record_key} has a download with no file_name.")

                    if download.file_path is None or download.file_path != "":
                        print(f"{model_record_key} has a download with a file_path.")

                    if download.file_url is None or download.file_url == "":
                        print(f"{model_record_key} has a download with no file_url.")
                        continue

                    if "civitai" in download.file_url:
                        download.known_slow_download = True
                    try:
                        host = urllib.parse.urlparse(download.file_url).netloc
                        download_hosts[host] = download_hosts.get(host, 0) + 1
                    except Exception as e:
                        print(f"{model_record_key} has a download with an invalid file_url.")
                        raise e

        return download_hosts


class LegacyControlnetConverter(LegacyConverterBase):
    def __init__(
        self,
        *,
        legacy_folder_path: str | Path = LEGACY_REFERENCE_FOLDER,
        target_file_folder: str | Path,
        debug_mode: bool = False,
        print_errors: bool = True,
    ):
        super().__init__(
            legacy_folder_path=legacy_folder_path,
            target_file_folder=target_file_folder,
            model_reference_type=consts.MODEL_REFERENCE_TYPE.CONTROLNET,
            debug_mode=debug_mode,
            print_errors=print_errors,
        )

    def normalize_and_convert(self) -> bool:
        """Normalize and convert the legacy controlnet files.

        Returns:
            bool: True if successful, False otherwise.
        """

        with open(self.legacy_database_path) as legacy_model_reference_file:
            raw_legacy_json_data = json.load(legacy_model_reference_file)

        all_model_records: dict[str, Legacy_Generic_ModelRecord] = {}

        return True


if __name__ == "__main__":
    sd_converter = LegacyStableDiffusionConverter(
        legacy_folder_path=Path(__file__).parent,
        target_file_folder=Path(__file__).parent.parent,
        debug_mode=False,
        print_errors=True,
    )
    sd_converter.normalize_and_convert()

    cn_converter = LegacyControlnetConverter(
        legacy_folder_path=Path(__file__).parent,
        target_file_folder=Path(__file__).parent.parent,
        debug_mode=False,
        print_errors=True,
    )
    cn_converter.normalize_and_convert()
