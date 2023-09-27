from functools import partial

import pandas as pd

from intake_esgf import ESGFCatalog

# As we aim to benchmark biogeochemical cycles, a basic requirement for inclusion in the
# ILAMB analysis is to have a carbon cycle. So we require certain variables to be
# present.
cat = ESGFCatalog().search(
    strict=True,
    activity_id="CMIP",
    experiment_id="historical",
    variable_id=["cSoil", "cVeg", "gpp", "lai", "nbp", "netAtmosLandCO2Flux"],
    frequency="mon",
    grid_label="gn",
    limit=2000,
)


# We want all the variables but `nbp` and `netAtmosLandCO2Flux` are the same variable.
# Some models will have both and some either.
def complete(df: pd.DataFrame) -> bool:
    # you must have all of these variables
    if set(["cSoil", "cVeg", "gpp", "lai"]).difference(df.variable_id.unique()):
        return False
    # either is sufficient
    if df.variable_id.isin(["nbp", "netAtmosLandCO2Flux"]).any():
        return True
    return False


ilamb_complete = partial(complete)
cat.remove_incomplete(ilamb_complete)

# For our anlaysis we only want a single ensemble member, so this will remove all but
# the smallest ensemble in terms of numeric value of the 4 integers in the `member_id`.
cat.remove_ensembles()

# Test lai for non-zero interannual variability
