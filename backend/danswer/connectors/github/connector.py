import time
import base64
from collections.abc import Iterator
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import cast
from pathlib import Path

from github import Github
from github import RateLimitExceededException
from github import Repository
from github import ContentFile
from github.Issue import Issue
from github.PaginatedList import PaginatedList
from github.PullRequest import PullRequest

from danswer.configs.app_configs import GITHUB_CONNECTOR_BASE_URL
from danswer.configs.app_configs import INDEX_BATCH_SIZE
from danswer.configs.constants import DocumentSource
from danswer.connectors.interfaces import GenerateDocumentsOutput
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.interfaces import SecondsSinceUnixEpoch
from danswer.connectors.models import ConnectorMissingCredentialError
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.utils.batching import batch_generator
from danswer.utils.logger import setup_logger
from danswer.connectors.github.utils import (_clone_repo, _compile_ignore_patterns, _list_files_to_ignore_in_repo,
                                             _process_repo, _remove_temp_repo, _is_valid_utf8)

logger = setup_logger()


_MAX_NUM_RATE_LIMIT_RETRIES = 5


def _sleep_after_rate_limit_exception(github_client: Github) -> None:
    sleep_time = github_client.get_rate_limit().core.reset.replace(
        tzinfo=timezone.utc
    ) - datetime.now(tz=timezone.utc)
    sleep_time += timedelta(minutes=1)  # add an extra minute just to be safe
    logger.info(f"Ran into Github rate-limit. Sleeping {sleep_time.seconds} seconds.")
    time.sleep(sleep_time.seconds)


def _get_batch_rate_limited(
    git_objs: PaginatedList, page_num: int, github_client: Github, attempt_num: int = 0
) -> list[Any]:
    if attempt_num > _MAX_NUM_RATE_LIMIT_RETRIES:
        raise RuntimeError(
            "Re-tried fetching batch too many times. Something is going wrong with fetching objects from Github"
        )

    try:
        objs = list(git_objs.get_page(page_num))
        # fetch all data here to disable lazy loading later
        # this is needed to capture the rate limit exception here (if one occurs)
        for obj in objs:
            if hasattr(obj, "raw_data"):
                getattr(obj, "raw_data")
        return objs
    except RateLimitExceededException:
        _sleep_after_rate_limit_exception(github_client)
        return _get_batch_rate_limited(
            git_objs, page_num, github_client, attempt_num + 1
        )


def _batch_github_objects(
    git_objs: PaginatedList, github_client: Github, batch_size: int
) -> Iterator[list[Any]]:
    page_num = 0
    while True:
        batch = _get_batch_rate_limited(git_objs, page_num, github_client)
        page_num += 1

        if not batch:
            break

        for mini_batch in batch_generator(batch, batch_size=batch_size):
            yield mini_batch


def _convert_pr_to_document(pull_request: PullRequest) -> Document:
    return Document(
        id=pull_request.html_url,
        sections=[Section(link=pull_request.html_url, text=pull_request.body or "")],
        source=DocumentSource.GITHUB,
        semantic_identifier=pull_request.title,
        # updated_at is UTC time but is timezone unaware, explicitly add UTC
        # as there is logic in indexing to prevent wrong timestamped docs
        # due to local time discrepancies with UTC
        doc_updated_at=pull_request.updated_at.replace(tzinfo=timezone.utc),
        metadata={
            "merged": str(pull_request.merged),
            "state": pull_request.state,
        },
    )


def _fetch_issue_comments(issue: Issue) -> str:
    comments = issue.get_comments()
    return "\nComment: ".join(comment.body for comment in comments)


def _convert_issue_to_document(issue: Issue) -> Document:
    return Document(
        id=issue.html_url,
        sections=[Section(link=issue.html_url, text=issue.body or "")],
        source=DocumentSource.GITHUB,
        semantic_identifier=issue.title,
        # updated_at is UTC time but is timezone unaware
        doc_updated_at=issue.updated_at.replace(tzinfo=timezone.utc),
        metadata={
            "state": issue.state,
        },
    )


def _convert_element_to_document(content: ContentFile, content_text) -> Document:
    # Set the timezone to UTC
    dt = datetime.strptime(content.last_modified, '%a, %d %b %Y %H:%M:%S %Z').replace(tzinfo=timezone.utc)
    return Document(
            id=content.html_url,
            sections=[Section(link=content.html_url, text=content_text)],
            source=DocumentSource.GITHUB,
            semantic_identifier=content.name,
            # updated_at is UTC time but is timezone unaware
            doc_updated_at=dt,
            metadata={
                "path": content.path,
            },
        )



class GithubConnector(LoadConnector, PollConnector):
    def __init__(
        self,
        repo_owner: str,
        repo_name: str,
        repo_branch: str,
        batch_size: int = INDEX_BATCH_SIZE,
        state_filter: str = "all",
        include_prs: bool = True,
        include_issues: bool = False,
        include_code: bool = True,
        download_repo: bool = True,
        temp_local_storage: str= '/temp'
    ) -> None:
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.repo_branch = repo_branch
        self.batch_size = batch_size
        self.state_filter = state_filter
        self.include_prs = include_prs
        self.include_issues = include_issues
        self.include_code = include_code
        self.github_client: Github | None = None
        self.download_repo = download_repo
        self.temp_local_storage = temp_local_storage

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        self.github_client = (
            Github(
                credentials["github_access_token"], base_url=GITHUB_CONNECTOR_BASE_URL
            )
            if GITHUB_CONNECTOR_BASE_URL
            else Github(credentials["github_access_token"])
        )
        return None

    def _get_github_repo(
        self, github_client: Github, attempt_num: int = 0
    ) -> Repository.Repository:
        if attempt_num > _MAX_NUM_RATE_LIMIT_RETRIES:
            raise RuntimeError(
                "Re-tried fetching repo too many times. Something is going wrong with fetching objects from Github"
            )

        try:
            return github_client.get_repo(f"{self.repo_owner}/{self.repo_name}")
        except RateLimitExceededException:
            _sleep_after_rate_limit_exception(github_client)
            return self._get_github_repo(github_client, attempt_num + 1)



    def _fetch_from_github(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> GenerateDocumentsOutput:

        if self.github_client is None:
            raise ConnectorMissingCredentialError("GitHub")

        repo = self._get_github_repo(self.github_client)

        # For large repos, Github rate limiting can be a problem and there's therefore an option here to use
        # the repo clone method to download the repo locally and then process it.
        if self.include_code and self.download_repo:
            doc_batch: list[Document] = []
            repo_path = _clone_repo(owner=self.repo_owner, repository=self.repo_name,token=os.environ['GITHUB_ACCESS_TOKEN'])
            # outputs a combined list of files to ignore looking for the .gitignore as well as an additional .gptignore file
            combine_ignore_lists = _compile_ignore_patterns(repo_path=repo_path, ignore_file_path=Path('.gptignore'), use_gitignore=True)
            # returns a list object of any file in the repo matching the ignore criteria
            files_to_ignore = _list_files_to_ignore_in_repo(combine_ignore_lists, repo_path)
            # traverses repo and creates
            doc_batch=_process_repo(repo_path, files_to_ignore, doc_batch)
            # _remove_temp_repo(repo_path)
            yield doc_batch

        # The more proper method of just calling the API to use the tree and get_contents method
        if self.include_code and not self.download_repo:
            branch = repo.get_branch(self.repo_branch)
            content_tree = repo.get_git_tree(branch.commit.sha, recursive=True)

            for git_tree_element in content_tree.tree:
                if git_tree_element.type != 'tree':
                    element_content = repo.get_contents(git_tree_element.path)
                    raw_bytes = base64.b64decode(element_content.content)
                    if _is_valid_utf8(raw_bytes):
                        decoded_text=raw_bytes.decode('utf-8')
                        element_doc = _convert_element_to_document(element_content,decoded_text)
                        doc_batch.append(element_doc)
                    else:
                        print("Not valid utf-8 encoded document - skipped: " + element_content.html_url)
            yield doc_batch


        if self.include_prs:
            pull_requests = repo.get_pulls(
                state=self.state_filter, sort="updated", direction="desc"
            )
            doc_batch: list[Document] = []
            for pr_batch in _batch_github_objects(
                pull_requests, self.github_client, self.batch_size
            ):

                for pr in pr_batch:
                    if start is not None and pr.updated_at < start:
                        yield doc_batch
                        return
                    if end is not None and pr.updated_at > end:
                        continue
                    doc_batch.append(_convert_pr_to_document(cast(PullRequest, pr)))
                yield doc_batch

        if self.include_issues:
            issues = repo.get_issues(
                state=self.state_filter, sort="updated", direction="desc"
            )

            for issue_batch in _batch_github_objects(
                issues, self.github_client, self.batch_size
            ):
                doc_batch = []
                for issue in issue_batch:
                    issue = cast(Issue, issue)
                    if start is not None and issue.updated_at < start:
                        yield doc_batch
                        return
                    if end is not None and issue.updated_at > end:
                        continue
                    if issue.pull_request is not None:
                        # PRs are handled separately
                        continue
                    doc_batch.append(_convert_issue_to_document(issue))
                yield doc_batch

    def load_from_state(self) -> GenerateDocumentsOutput:
        return self._fetch_from_github()

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        start_datetime = datetime.utcfromtimestamp(start)
        end_datetime = datetime.utcfromtimestamp(end)

        # Move start time back by 3 hours, since some Issues/PRs are getting dropped
        # Could be due to delayed processing on GitHub side
        # The non-updated issues since last poll will be shortcut-ed and not embedded
        adjusted_start_datetime = start_datetime - timedelta(hours=3)

        epoch = datetime.utcfromtimestamp(0)
        if adjusted_start_datetime < epoch:
            adjusted_start_datetime = epoch

        return self._fetch_from_github(adjusted_start_datetime, end_datetime)


if __name__ == "__main__":
    import os

    connector = GithubConnector(
        repo_owner=os.environ["REPO_OWNER"],
        repo_name=os.environ["REPO_NAME"],
        repo_branch=os.environ["REPO_BRANCH"]
    )
    connector.load_credentials(
        {"github_access_token": os.environ["GITHUB_ACCESS_TOKEN"]}
    )
    document_batches = connector.load_from_state()
    print(next(document_batches))
