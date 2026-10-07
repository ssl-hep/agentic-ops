# EWKit knowledge card

Written and kept by the agentic-ops team from EWKit master at 17c998c. Review it at each EWKit tag.

## What a run is

EWKit produces analysis n-tuples with the ATLAS CP algorithms (AnalysisBase 25.2.108). Each
analysis is a folder under `data/` with a YAML config and lists of DAOD datasets:

- `data/<Folder>/config.yaml`, plus other `config*.yaml` files picked with `--configName`
- `data/<Folder>/DAODs/Run2_<sample>.txt` and `Run3_<sample>.txt`: one Rucio dataset per line;
  `#` lines are group headings. Sample names vary by folder: `bkg`, `data`, `signal`, `sig`
  (2L3L), `bkg_syst` (CompressedSleptons). Only use names that exist in the folder.
- `data/common` is not an analysis; it holds shared background groups.

A job runs `EWRun.py --analysis <Folder> [--configName <file>] [--doSys|--noSys] [--noFilter]
[--noThinning]` on one dataset's files. One HTCondor cluster per dataset, as EWGridRun.py
submits one PanDA task per dataset.

## RunSpec options (they mirror EWGridRun.py)

- `analysis`: the folder name. `--analysis` must name the folder because EWRunScript.py changes
  settings per analysis, and per Run 2 / Run 3, at run time.
- `config`: `--configName`; `config.yaml` unless the request names another file.
- `runs`: `Run2`, `Run3` or both (`--runs`). Only runs with sample lists in the folder.
- `sample_lists`: sample names, combined with each run to pick `DAODs/<Run>_<sample>.txt`.
- `systematics`: `do` (`--doSys`), `no` (`--noSys`) or `config-default`. Some configs turn
  systematics on by default (`runSystematics: True` in CompressedSleptons, DisplacedTrack,
  SUSYEWK1L2L); then they run without `--doSys`, but output names get `_SYS` only with `--doSys`.
- `no_filter` (`--noFilter`), `no_thinning` (`--noThinning`): off unless asked for.
- `include` (`-w`) and `skip` (`--skip`): Python regular expressions matched anywhere in the
  dataset name. Skipping an r-tag: `_r17610_`. Skipping a DSID: `\.601183\.`.
- `purpose`: `production` for a full production (must be a git tag), `test` for a test-sized
  run from a branch or commit (outputs get a test suffix).
- `memory_mb`: 2000 unless there is a reason to change it.
- `files_per_job`: leave null; it is worked out at Gate 1 from Rucio file sizes.

## Refs and tags

- Full productions must come from a clean, pushed tag; the tag goes into output names.
- Protected `vX.Y` tags are created by the maintainers, but most recent tags are per analysis,
  such as `EFP_v6.6`, `EWK1L2L_v6.4` and `SlepWjetFF_v6.0`. Which prefix belongs to which folder
  is not written down (`EWK1L2L` probably SUSYEWK1L2L, `SlepWjetFF` probably SleptonWjetFF):
  check with `list_refs` and state any guess as an assumption.
- Keep the ref exactly as the person wrote it. Code resolves it to a commit.

## Output names

`user.<user>.<analysis>.<DSID>.<sample>.<campaign>.<tag>`, with `__NOFILTER`, `__SYS` or
`__NOFILTER_SYS` appended for `--noFilter` / `--doSys`. The campaign comes from the dataset's
r-tag through a fixed map (mc20a/d/e, mc23a/d/e); a dataset whose r-tag is not in the map cannot
be named, and EWGridRun.py stops on it.

## Extend

A config may start with `Extend: '../Other/config.yaml'` and override some blocks. EWKit merges
it into `config_extended.yaml` written next to the config in the installed `data/` folder, which
fails if the image is read-only.

## Folders on master at 17c998c

- Runnable: 2L3L, CompressedSleptons, DiTaus, DiTauStudies, DisplacedTrack, RadiativeDecays,
  SleptonDijetFF and SleptonWjetFF (both extend CompressedSleptons).
- Blocked: SUSYEWK1L2L needs TopCPToolkit, which the standard image lacks.
- Config only (no sample lists): RadiativeCSE, RadiativeEFPFakeFactor.
- CI runs only common, CompressedSleptons, RadiativeDecays and DisplacedTrack on example files.
- Most lists are DAOD_PHYS; 2L3L uses DAOD_PHYSLITE, DisplacedTrack DAOD_SUSY20.
- Signal lists can be very large (over 3,400 datasets in 2L3L and SUSYEWK1L2L).

These facts may have changed at the commit being used; the tools show the current state.
