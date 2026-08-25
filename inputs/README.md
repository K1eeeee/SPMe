# Input artifacts

- `dfn_calibrated_parameters.json`: byte-for-byte provenance copy from the
  active DFN run; the SPMe CLI will reject it as a direct SPMe artifact.
- `spme_transferred_parameters.json`: the same numerical factors, with an SPMe
  target and explicit DFN source metadata. Use this file for the matching SPMe
  verification run.
