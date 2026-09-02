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

Processor state schema v2 keeps JSON metadata plus an atomic compressed `alignment_state.npz` sidecar. The sidecar is verified by method, frame sequence, SHA-256 checksum, shapes, dtypes, and finite values. A missing or corrupt sidecar fails closed. Schema v1 is accepted only with `alignment_method: none`.

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
