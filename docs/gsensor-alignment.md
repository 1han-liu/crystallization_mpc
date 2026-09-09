# GSensor frame alignment

## Purpose and scope

The GSensor service can optionally register each post-baseline camera frame before growth-line detection. The feature is native to the Python processing pipeline; the standalone scripts and hard-coded image directories from the alignment reference repository are not runtime dependencies.

The default is `alignment_method: none`, which preserves the original measurement path. The available values are:

```text
none, centroid, fft, kalman, loftr, sift, optical_flow, ecc
```

The algorithms retain their reference-specific estimators. They only share a service interface, state contract, diagnostics schema, and the convention that the exposed transform maps the current frame into the initial aligned reference coordinate system.

## Runtime data flow

```text
camera image
  -> one YOLO inference
  -> raw mask + measurement mask
  -> selected frame aligner
  -> aligned image + aligned measurement mask
  -> one edge-mask build
  -> shared u/v Hough processing
  -> growth-rate EKF
```

YOLO is not repeated for u and v. LoFTR and SIFT derive their matching masks from the same raw and measurement masks; their `close75 -> erode25 -> soft50 -> inner>0.95` masks are matching aids and never replace the authoritative measurement mask.

## Selection and capabilities

Both parameter pages render `alignment_method` as a select control. Parameters remain locked while an experiment is active. GSensor also exposes:

```text
GET /api/alignment/capabilities
```

The response identifies unavailable methods and supplies a reason. The GSensor page disables unavailable options. A selected method is never silently changed to `none`.

## Failure and recovery

Every frame records method, success, fallback use, error, translation, rotation, runtime, image residuals, and method-specific match statistics. If selected alignment fails, the frame is invalid and the alignment, u/v, and EKF states are not advanced.

GSensor runtime snapshots and nested processor snapshots now use **unified extended schema v1 only**. This is a project-specific extension of the original v1, not a promise that older program versions can read aligned snapshots. The `alignment` metadata (method, initialization flag, and sidecar descriptor) is retained, together with an atomic compressed `alignment_state.npz` sidecar. The sidecar is verified by method, frame sequence, SHA-256 checksum, shapes, dtypes, and finite values. A missing or corrupt sidecar fails closed. Old v1 records that lack the `alignment` field may still resume only with `alignment_method: none`; the reader never invents alignment history.

Runtime save/load/restore reject schema v2, including a v2 nested processor. Before upgrading an existing run, stop GSensor and explicitly convert its snapshot with `scripts/migrate_gsensor_state_v1.py`. With an explicit path to `gsensor_processing_state.json`, the tool defaults to a dry-run; `--apply` creates a unique byte-for-byte `.bak` alongside the snapshot and atomically converts only the outer and nested processor version labels. All other data and sidecar references remain unchanged, and `alignment_state.npz` is not rewritten. Repeat execution is a no-op for an already-converted file. Do not convert Controller, experiment-selection, or unrelated report schemas.

After conversion, restart GSensor and verify the same run, initialization session, processed-image identities, and alignment state are recovered. Keep the backup until recovery is verified. Rollback requires stopping the service and restoring both the prior code and its matching snapshot; changing a version label alone does not make old software understand the extended format.

## LoFTR deployment

`services/gsensor/Dockerfile` uses `requirements-gsensor.txt`; Central and Controller continue using the smaller shared image. The GSensor build downloads the pinned Kornia outdoor checkpoint and verifies its SHA-256 digest. Runtime model construction refuses to download a missing checkpoint. CPU execution is supported and CUDA is used automatically when the installed Torch build exposes it.

## Offline evaluation

Alignment-only evaluation of an image directory is available with:

```bash
python -m crystallization_mpc.apps.gsensor.alignment.evaluation \
  --input /path/to/images \
  --output .runtime/alignment-evaluation
```

Segmentation artifacts are cached once, then shared by all methods. JSON and CSV include completion, failures, transforms, frame-to-frame jumps, residuals, runtime, RSS, GPU allocation, and the fraction of frames below `dt_G`. Hough and growth-rate acceptance require a valid initialization/calibration and are deliberately reported as `NOT RUN` by this alignment-only command. The existing offline `DSCGR` entry point uses the same `GrowthRateProcessor` as the online service and should be used for calibrated end-to-end acceptance.

## Verification boundary

Automatic and external-image tests establish software integration and offline behavior. They do not replace a real crystallization experiment. Report real experimental acceptance separately as `PASS`, `FAIL`, or `NOT RUN`.
