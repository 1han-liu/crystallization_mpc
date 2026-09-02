# Source Baseline

This standalone working copy was created from the tracked working-tree files of
`MPCrystal_original_python` before alignment integration began.

## Platform source

- Repository: `https://github.com/1han-liu/crystallization_mpc.git`
- Branch: `controller`
- Commit: `d06c2b4d5b35f0201554c6ae5c978930eaf3df6f`
- Git history copied: no

The working tree contained three intentional path-only changes, and their
current contents are part of this baseline:

- `docs/controller-reference-validation.md`
- `tests/controller/fixtures/baseline_manifest.json`
- `tests/controller/matlab_harness/generate_golden_fixtures.m`

Each change updates the frozen MATLAB reference directory from
`MPCrystal_original` to `MPCrystal_original_matlab`.

## Alignment reference

- Local reference: `align`
- Branch: `main`
- Commit: `c1d08588cf3bae28c29af366827d07249ab388f6`

The algorithm source files were pinned by SHA-256 before porting:

| Method | Source file | SHA-256 |
| --- | --- | --- |
| Centroid | `python/3_mass-center.py` | `1c49f99c8a013edb3adddce33c40e1b3fefd162a59498de209629293ac49e61b` |
| FFT | `python/3_fft.py` | `6bae85c6a3f976d7ad47e579cd6bf8b7f8bf2516d39e22c63e16db3b3657caf5` |
| Kalman | `python/3_kalman.py` | `cc98df5c632ddb59b6994d19001f100098fc174d68845bc00d5ec398895368d0` |
| LoFTR | `python/3_loftr.py` | `ca1f96bb39d26f93f78aba47457918c84b061d83f56d3653c4f8243473da0a13` |
| SIFT | `python/3_sift.py` | `16fdd04912ff7aba9ec7587a2cdb297bc12bee860bac906586e5679c9c95811e` |
| Optical flow | `python/3_optical-flow.py` | `fd3503a39925d477e5980937d291e93e4bbdc4d61e7720254b91f2b5cf362a05` |
| ECC | `python/3_ecc.py` | `b2ff666424d0374ecf70f84d4fca6d0973a3ff0a41053afe0a7e12be07caeeb5` |

Only the in-memory alignment algorithms are ported. The reference repository's
Git metadata, images, experiment outputs, numbered command-line scripts, and
standalone GUI are outside this project.
