// coord-mcp — réécriture Kotlin, en parallèle du service Python (strangler).
//
// Aucune migration de données : ce module parle le MÊME schéma SQLite
// (~/.coord-mcp/state.db). Les 2090 work items restent en place et les deux
// services coexistent jusqu'à la bascule, outil par outil.
//
// Rigueur imposée par le build, pas par la discipline :
//   - `explicitApi()`      : toute API publique est explicitement déclarée ;
//   - `allWarningsAsErrors`: un avertissement casse la compilation ;
//   - SQLDelight           : le SQL est vérifié à la COMPILATION contre le schéma.
plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.detekt)
    application
    // Génération jOOQ depuis la base PostgreSQL réelle (voir le bloc `jooq` plus bas).
    id("org.jooq.jooq-codegen-gradle") version "3.20.19"
}

repositories { mavenCentral() }

dependencies {
    implementation(libs.kotlinx.coroutines.core)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.arrow.core)
    implementation(libs.mcp.server)
    implementation(libs.mcp.client)
    implementation(libs.ktor.client.cio)
    implementation(libs.ktor.server.cio)
    implementation(libs.kotlinx.io.core)

    // Cible de la migration : PostgreSQL. Le driver sert aussi à la génération.
    implementation(libs.jooq)
    implementation(libs.postgresql)
    jooqCodegen(libs.postgresql)

    runtimeOnly(libs.logback.classic)

    testImplementation(kotlin("test"))
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.kotest.runner)
    testImplementation(libs.kotest.assertions)
    testImplementation(libs.kotest.property)
}

kotlin {
    jvmToolchain(21)

    // Contrats : pas d'API publique implicite, pas d'avertissement toléré.
    explicitApi()
    compilerOptions {
        allWarningsAsErrors.set(true)
    }
}

detekt {
    buildUponDefaultConfig = true
    config.setFrom(files("$projectDir/detekt.yml"))
}

// Exécutable autonome : `gradle installDist` produit `build/install/…/bin/coord-mcp-kotlin`,
// lançable avec les variables d'environnement de configuration. C'est ce qui permet
// de PARLER le protocole pour de vrai, au lieu de le supposer.
application {
    mainClass.set("coordmcp.MainKt")
}

// ── jOOQ — types générés depuis la base PostgreSQL RÉELLE ───────────────────
//
// Le schéma n'est déclaré qu'UNE fois : dans les migrations SQL. jOOQ lit la
// base et génère les classes typées. Aucune dérive possible entre le code et la
// base — contrairement à une déclaration dupliquée, dont on a observé la
// conséquence : une colonne (`triggers_ci`) présente en base et absente du code.
//
// Prérequis : la base doit exister et être à jour.
//   psql -d coord_mcp -f migrations/pg/001_init.sql
jooq {
    configuration {
        jdbc {
            driver = "org.postgresql.Driver"
            url = "jdbc:postgresql://127.0.0.1:5432/coord_mcp"
            user = System.getProperty("user.name")
            password = ""
        }
        generator {
            database {
                name = "org.jooq.meta.postgres.PostgresDatabase"
                inputSchema = "public"
            }
            target {
                packageName = "coordmcp.db.jooq"
                directory = "build/generated-src/jooq/main"
            }
        }
    }
}

sourceSets {
    main {
        java.srcDir("build/generated-src/jooq/main")
    }
}

tasks.test {
    // Les tests `live` exigent un service MCP en face. Ils ne sont PAS ici :
    // une suite qui les inclut doit pouvoir les sauter, et un saut ressemble
    // à un succès. Une tâche absente, elle, se voit. Ils vivent dans
    // `liveContractTest`.
    useJUnitPlatform { excludeTags("live") }
    // Audit sur données réelles : opt-in, lit une COPIE de la base.
    environment("COORD_MCP_DB_COPY", System.getenv("COORD_MCP_DB_COPY") ?: "")
    testLogging { events("passed", "failed", "skipped") }
}

/**
 * Contrat VIVANT — exige un service MCP sur COORD_MCP_LIVE_URL.
 *
 * Tâche SÉPARÉE, et non variable d'environnement, pour une raison de fond :
 * une variable s'oublie, une tâche absente se voit. `./gradlew test` ne peut
 * donc plus produire un vert en ayant sauté la vérification de contrat — il
 * faut nommer `liveContractTest`, et cette tâche échoue si le service manque.
 *
 * Le requirement est posé ICI, pas par l'appelant : aucun appelant ne peut
 * l'omettre, et aucun test de cette tâche n'a de branche de saut.
 */
tasks.register<Test>("liveContractTest") {
    description = "Exige un service MCP en face. Aucun saut autorisé."
    group = "verification"
    testClassesDirs = sourceSets["test"].output.classesDirs
    classpath = sourceSets["test"].runtimeClasspath
    useJUnitPlatform { includeTags("live") }
    systemProperty("coordmcp.requireLive", "true")
    environment(
        "COORD_MCP_LIVE_URL",
        System.getenv("COORD_MCP_LIVE_URL") ?: "http://127.0.0.1:8015/mcp",
    )
    testLogging { events("passed", "failed", "skipped") }
}
