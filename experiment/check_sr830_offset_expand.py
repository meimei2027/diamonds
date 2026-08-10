"""
check_sr830_offset_expand.py -- query the SR830's current X/Y OEXP
(offset/expand) settings without running a full rabi.py sweep.

setup_lock_in() (rabi.py) never called lia.reset() at connect time, so any
offset/expand left over from a previous session (another script, a manual
front-panel adjustment) silently persisted across runs -- a nonzero offset
or >1x expand shrinks the SR830's usable output range, making a nuisance
"output overload" LIAS trip far easier from an otherwise harmless
transient, unrelated to the actual input signal. See notes.md's "spurious
off-resonance/no-MW-near-sample signal" entry -- this is how a real
output_overload=True, input_overload=False spike was caught during that
investigation. setup_lock_in() now resets both channels to 0%/1x on every
run, but this script is for checking/spot-verifying the instrument's state
directly, independent of running rabi.py.

Usage:
    python check_sr830_offset_expand.py
"""
from sr830 import SR830

# See notes.md -- GPIB bus numbering isn't stable, confirm with
# pyvisa.ResourceManager().list_resources() if this doesn't match.
SR830_RESOURCE = "GPIB2::2::INSTR"


def main():
    lia = SR830(SR830_RESOURCE, debug=True)

    print(f"{'channel':>8} {'offset (%)':>12} {'expand':>8}")
    any_nonzero = False
    for channel, name in [(1, "X"), (2, "Y"), (3, "R")]:
        offset_percent, expand = lia.get_offset_expand(channel)
        print(f"{name:>8} {offset_percent:>12.3f} {expand:>7}x")
        if offset_percent != 0.0 or expand != 1:
            any_nonzero = True

    if any_nonzero:
        print("\nWARNING: at least one channel has a nonzero offset or >1x "
              "expand -- this shrinks the SR830's usable output range and "
              "makes a nuisance output-overload trip much easier to hit "
              "from an otherwise harmless transient. rabi.py's "
              "setup_lock_in() now resets both to 0%/1x on every run, but "
              "if you're running something else (cw_odmr_lock_in.py, "
              "manual front-panel use) that doesn't, this can persist.")
    else:
        print("\nLooks clean -- 0% offset, 1x expand on both X and Y.")

    lia.go_to_local()
    lia.close()


if __name__ == "__main__":
    main()
