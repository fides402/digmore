package com.digmore.newpipecli

/** All diagnostic output goes to stderr — stdout is reserved for the JSON result. */
fun err(msg: String) = System.err.println("[newpipe-cli] $msg")
