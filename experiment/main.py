import dg535
import ks33600a
import sdg1062x
import generate_arb

EXCITATION = 5e-6
PI = 60e-9
PI_2 = PI / 2
READOUT = 300e-9
AOM_DELAY = 1e-6
TAU = 60e-9
MICROWAVE = PI + TAU + PI_2 + TAU + PI
SINGLE_CYCLE = AOM_DELAY + EXCITATION + MICROWAVE + AOM_DELAY + READOUT
KS33600A_MAX_SAMPLE_RATE = 1e9
SDG1062X_MAX_SAMPLE_RATE = 30e6

# does it take extra time to save to memory?

def prepare_files():
    generate_arb.generate_ks33600a("waveforms/test.csv", 77e6, \
        [0, \
         EXCITATION, \
         AOM_DELAY + EXCITATION + MICROWAVE, \
         AOM_DELAY + EXCITATION + MICROWAVE + READOUT, \
         SINGLE_CYCLE, \
         SINGLE_CYCLE + EXCITATION, \
         SINGLE_CYCLE + AOM_DELAY + EXCITATION + MICROWAVE, \
         SINGLE_CYCLE + AOM_DELAY + EXCITATION + MICROWAVE + READOUT, \
        ], [0, 1e-6], SINGLE_CYCLE * 2, sample_rate=KS33600A_MAX_SAMPLE_RATE)
    
    # generate_arb.generate_sdg1062x("waveforms/sdg1062x.csv", \
    #     [AOM_DELAY + EXCITATION, \
    #      AOM_DELAY + EXCITATION + PI, \
    #      AOM_DELAY + EXCITATION + PI + TAU, \
    #      AOM_DELAY + EXCITATION + PI + TAU + PI_2, \
    #      AOM_DELAY + EXCITATION + PI + TAU + PI_2 + TAU, \
    #      AOM_DELAY + EXCITATION + PI + TAU + PI_2 + TAU + PI, \
    #     ], SINGLE_CYCLE * 2,
    #     sample_rate=SDG1062X_MAX_SAMPLE_RATE)

    generate_arb.generate_sdg1062x("waveforms/sdg1062x.csv", \
        [0, 1e-6
        ], 10e-6,
        sample_rate=SDG1062X_MAX_SAMPLE_RATE)

    # edges_ch2

if __name__ == "__main__":
    generate = True
    if generate == True:
        prepare_files()
    # note there is a settling time of 2 seconds
    dg = dg535.DG535("GPIB0::15::INSTR")

    dg.configure_sequence(
        t0_to_a=0,
        a_to_b=AOM_DELAY + EXCITATION,
        b_to_c=1e-6, # longer pulse to trigger on
        t_cycle=SINGLE_CYCLE * 2
    )
    dg.close()

    # trigger on A
    # ks = ks33600a.KS33600A("USB0::0x0957::0x5707::MY53800810::INSTR")
    # ks.upload_csv("waveforms/test.csv")

    # measured on scope: seems about 85% of specified peak-to-peak gets through, ex. 100 mV => 85 mV
    # ks.run(vpp=0.5)
    # check later if running at 50 Ohm
    # ks.run_alignment()

    # ks.close()

    # trigger on B
    # remember to set to 50 ohm later
    # amplitude is pretty accurate at high Z
    # idle after the waveform is done is at 700 mV
    # unavoidable delay ~380ns from the trigger
    sdg = sdg1062x.SDG1062X("USB0::0xF4EC::0x1103::SDG1XDDX6R5043::INSTR")
    sdg.upload_csv("waveforms/sdg1062x.csv")
    sdg.run(vpp=2)
    sdg.close()

    # hp: page 3-110
    # high above 3V, low below 0.5V

# 


