"""The impure shell.

Transports, the scheduler, the reconciler, the event seam and persistence: everything that
touches a network, a disk or the real clock lives here and nowhere else. The shell is the sole
writer to the loads it manages. Vendor knowledge lives in its adapters; the core never learns
what a device is.
"""
