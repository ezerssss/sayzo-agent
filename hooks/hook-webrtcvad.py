# Override the contrib hook-webrtcvad.py which calls copy_metadata('webrtcvad')
# and fails because we use webrtcvad-wheels (different distribution name,
# same module name).  The module itself needs no special treatment.
