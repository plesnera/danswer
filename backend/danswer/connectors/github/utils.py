#!/usr/bin/env python3

import os
import json
import math
import shutil
import random as rnd
from git import Repo
from pathlib import Path
from typing import List
from danswer.configs.constants import DocumentSource
from danswer.connectors.models import Document
from danswer.connectors.models import Section


# Utils to use when importing very large repos that will exceed the standard 5000 'requests pr. hour'
# rate limit on Github.
# These utils downloads a repo to a temporary folder and provides an iterator to be used instead of API calls


# helper function to create a unique temporary folder
def _create_folder(temp_storage_path: Path, repository: str) -> Path:
    folder = temp_storage_path / str(hash(rnd.random())) / Path(repository)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _clone_repo(owner: str, repository: str, token: str, temp_storage_path: Path = Path('temp')) -> Path:
    destination_path = _create_folder(temp_storage_path, repository)
    repo_url = f"https://{token}@github.com/{owner}/{repository}.git"
    Repo.clone_from(repo_url, str(destination_path))
    return destination_path


def _read_ignore_file(ignore_file_path: Path) -> List[str]:
    ignore_list = []
    if ignore_file_path.is_file():
        with ignore_file_path.open('r') as file:
            for line in file:
                line = line.strip()
                if line == '' or line.startswith('#'):
                    continue
                if line.endswith('/'):
                    line = line + '**'
                line = line.lstrip('/')
                ignore_list.append(line)
    return ignore_list


def _windows_to_unix_path(windows_path: str) -> str:
    return windows_path.replace('\\', '/')


def _compile_ignore_patterns(repo_path: Path, ignore_file_path: Path, use_gitignore: bool) -> List[str]:
    ignore_list=[]
    if ignore_file_path:
        ignore_list = _read_ignore_file(ignore_file_path)

    if use_gitignore:
        gitignore_path = repo_path / '.gitignore'
        gitignore_list = _read_ignore_file(gitignore_path)
        ignore_list.extend(gitignore_list)

    final_ignore_list = [
        os.path.join(repo_path, pattern) if os.path.isdir(os.path.join(repo_path, pattern)) else pattern for pattern in
        ignore_list]
    return final_ignore_list


def _process_file(file_path: Path, repo_root: Path, repo_object:list[Document]) -> Document:
    with file_path.open('r', encoding='utf-8', errors='ignore') as file:
        contents = file.read()
        relative_path = file_path.relative_to(repo_root).as_posix()
        if _is_valid_utf8(contents.encode('utf8')):
            return Document(
                id=file_path.as_posix(),
                sections=[Section(link=relative_path, text=contents or "")],
                source=DocumentSource.GITHUB,
                semantic_identifier=file_path.name,
                # updated_at is UTC time but is timezone unaware
                metadata={
                },
            )
        else:
            pass

def _process_repo(repo_path: Path, files_to_ignore: List[Path], repo_object: list[Document]) -> list[Document]:
    for element in repo_path.rglob('*'):
        if element not in files_to_ignore:
            if element.is_file():
                repo_object.append(_process_file(element, repo_path, repo_object))
    return repo_object


def _is_valid_utf8(byte_string):
    try:
        byte_string.decode('utf-8')
        return True
    except UnicodeDecodeError:
        return False


def _list_files_to_ignore_in_repo(ignore_list: List[str], repo_source_path: Path):
    file_list = []
    for ignore_item in ignore_list:
        matches = repo_source_path.rglob(ignore_item)
        for match in matches:
            if match:
                file_list.append(match)
    return file_list


def _remove_temp_repo(repo_path: Path) -> None:
    shutil.rmtree(repo_path.parent)
    os.rmdir('temp')
    return
