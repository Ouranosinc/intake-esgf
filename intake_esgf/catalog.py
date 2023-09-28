import logging
import time
import warnings
from pathlib import Path
from typing import Callable, Union

import pandas as pd
import xarray as xr
from datatree import DataTree
from globus_sdk import SearchAPIError, SearchClient
from tqdm import tqdm

from intake_esgf.core import (
    GlobusESGFIndex,
    SolrESGFIndex,
    combine_results,
    response_to_https_download,
    response_to_local_filelist,
)

warnings.simplefilter("ignore", category=xr.SerializationWarning)

# setup logging, not sure if this belongs here but if the catalog gets used I want logs
# dumped to this location.
local_cache = Path.home() / ".esgf"
local_cache.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("intake-esgf")
log_file = local_cache / "esgf.log"
if not log_file.is_file():
    log_file.touch()
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(
    logging.Formatter(
        "\x1b[36;20m%(asctime)s \x1b[36;32m%(funcName)s()\033[0m %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logger.addHandler(file_handler)
logger.setLevel(logging.INFO)


class ESGFCatalog:
    def __init__(self):
        self.indices = [
            SolrESGFIndex(),
            SolrESGFIndex("esgf-node.ornl.gov", distrib=False),
            GlobusESGFIndex(),
        ]
        self.df = None  # dataframe which stores the results of the last call to search
        self.esgf_data_root = None  # the path where the esgf data already exists
        self.local_cache = Path.home() / ".esgf"  # the path to the local cache

    def __repr__(self):
        if self.df is None:
            return "Perform a search() to populate the catalog."
        return self.unique().__repr__()

    def unique(self) -> pd.Series:
        """Return the the unique values in each facet of the search."""
        out = {}
        for col in self.df.drop(columns=["id", "version"]).columns:
            out[col] = self.df[col].unique()
        return pd.Series(out)

    def model_groups(self) -> pd.Series:
        """Return counts for unique combinations of (source_id,member_id,grid_label)."""
        return self.df.groupby(["source_id", "member_id", "grid_label"]).count()[
            "variable_id"
        ]

    def search(self, **search: Union[str, list[str]]):
        """Populate the catalog by specifying search facets and values."""

        dfs = []
        for index in self.indices:
            search_time = time.time()
            print(f"{index}...", end=" ")
            try:
                df = index.search(**search)
            except (ValueError, SearchAPIError):
                pass
            search_time = time.time() - search_time
            print(f"{search_time:.2f}")
            dfs.append(df)
        self.df = combine_results(dfs)
        return self

    def set_esgf_data_root(self, root: Union[str, Path]) -> None:
        """Set the root directory of the ESGF data local to your system.

        It may be that you are working on a resource that has direct access to a copy of
        the ESGF data. If you set this root, then when calling `to_dataset_dict()`, we
        will check if the requested dataset is available through direct access.

        """
        if isinstance(root, str):
            root = Path(root)
        assert root.is_dir()
        self.esgf_data_root = root

    def to_dataset_dict(
        self,
        minimal_keys: bool = True,
        ignore_facets: Union[None, str, list[str]] = None,
        separator: str = ".",
    ) -> dict[str, xr.Dataset]:
        """Return the current search as a dictionary of datasets.

        By default, the keys of the returned dictionary are the minimal set of facets
        required to uniquely describe the search. If you prefer to use a full set of
        facets, set `minimal_keys=False`. You can also specify

        Parameters
        ----------
        minimal_keys
            Disable to return a dictonary whose keys are formed using all facets, by
            default we use a minimal set of facets to build the simplest keys.
        ignore_facets
            When constructing the dictionary keys, which facets should we ignore?
        separator
            When generating the keys, the string to use as a seperator of facets.
        """
        if self.df is None or len(self.df) == 0:
            raise ValueError("No entries to retrieve.")
        # Prefer the esgf data root if set, otherwise check the local cache for the
        # existence of files.
        data_root = (
            self.esgf_data_root if self.esgf_data_root is not None else self.local_cache
        )
        # The keys returned will be just the items that are different.
        output_key_format = []
        if ignore_facets is None:
            ignore_facets = []
        if isinstance(ignore_facets, str):
            ignore_facets = [ignore_facets]
        ignore_facets += [
            "version",
            "data_node",
            "globus_subject",
        ]  # these we always ignore
        for col in self.df.drop(columns=ignore_facets):
            if minimal_keys:
                if not (self.df[col].iloc[0] == self.df[col]).all():
                    output_key_format.append(col)
            else:
                output_key_format.append(col)
        # Form the returned dataset in the fastest way possible
        ds = {}
        for _, row in tqdm(
            self.df.iterrows(),
            unit="dataset",
            unit_scale=False,
            desc="Loading datasets",
            ascii=True,
            total=len(self.df),
        ):
            response = SearchClient().post_search(
                self.index_id,
                {
                    "q": "",
                    "filters": [
                        {
                            "type": "match_any",
                            "field_name": "dataset_id",
                            "values": [row.globus_subject],
                        }
                    ],
                    "facets": [],
                    "sort": [],
                },
                limit=1000,
            )
            file_list = []
            # 1) Look for direct access to files
            try:
                file_list = response_to_local_filelist(response, data_root)
            except FileNotFoundError:
                pass
            # 2) Use THREDDS links, but there are none in this index and so we will put
            #    this on the list to do.

            # 3) Use Globus for transfer? I know that we could use the sdk to
            #    authenticate but I am not clear on if we could automatically setup the
            #    current location as an endpoint.

            # 4) Use the https links to download data locally.
            if not file_list:
                file_list = response_to_https_download(response, self.local_cache)

            # Now open datasets and add to the return dictionary
            key = separator.join([row[k] for k in output_key_format])
            if len(file_list) == 1:
                ds[key] = xr.open_dataset(file_list[0])
            elif len(file_list) > 1:
                ds[key] = xr.open_mfdataset(file_list)
            else:
                ds[key] = "Could not obtain this file."
        return ds

    def to_datatree(
        self,
        minimal_keys: bool = True,
        ignore_facets: Union[None, str, list[str]] = None,
    ) -> DataTree:
        """Return the current search as a datatree.

        Parameters
        ----------
        minimal_keys
            Disable to return a dictonary whose keys are formed using all facets, by
            default we use a minimal set of facets to build the simplest keys.
        ignore_facets
            When constructing the dictionary keys, which facets should we ignore?

        See Also
        --------
        `to_dataset_dict`

        """
        return DataTree.from_dict(
            self.to_dataset_dict(
                minimal_keys=minimal_keys, ignore_facets=ignore_facets, separator="/"
            )
        )

    def remove_incomplete(self, complete: Callable[[pd.DataFrame], bool]):
        """Remove the incomplete search results as defined by the `complete` function.

        While the ESGF search results will return anything matching the criteria, we are
        typically interested in unique combinations of `source_id`, `member_id`, and
        `grid_label`. Many times modeling groups upload different realizations but they
        do not contain all the variables either by oversight or design. This function
        will internally group the results by these criteria and then call the
        user-provided `complete` function on the grouped dataframe and remove entries
        deemed incomplete.

        """
        for lbl, grp in self.df.groupby(["source_id", "member_id", "grid_label"]):
            if not complete(grp):
                self.df = self.df.drop(grp.index)
        return self

    def remove_ensembles(self):
        """Remove higher numeric ensembles for each `source_id`.

        Many times an ESGF search will return possible many ensembles, but you only need
        1 for your analysis, usually the smallest numeric values in the `member_id`.
        While in most cases it will simply be `r1i1p1f1`, this is not always the case.
        This function will select the *smallest* `member_id` (in terms of the smallest 4
        integer values) for each `source_id` in your search and remove all others.

        """
        for source_id, grp in self.df.groupby("source_id"):
            member_id = "r{}i{}p{}f{}".format(
                *(
                    grp.member_id.str.extract(r"r(\d+)i(\d+)p(\d+)f(\d+)")
                    .sort_values([0, 1, 2, 3])
                    .iloc[0]
                )
            )
            self.df = self.df.drop(grp[grp.member_id != member_id].index)
        return self
