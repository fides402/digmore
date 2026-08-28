plugins {
    kotlin("jvm") version "1.9.24"
    application
}

repositories {
    google()
    mavenCentral()
    maven("https://jitpack.io")
}

dependencies {
    implementation("com.github.TeamNewPipe:NewPipeExtractor:v0.26.5")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.json:json:20240303")
}

application {
    mainClass.set("com.digmore.newpipecli.MainKt")
}

kotlin {
    jvmToolchain(17)
}

tasks.named<CreateStartScripts>("startScripts") {
    // Keep the generated launcher script name short/stable — generate_cli.py
    // invokes build/install/newpipe-cli/bin/newpipe-cli directly.
    applicationName = "newpipe-cli"
}
