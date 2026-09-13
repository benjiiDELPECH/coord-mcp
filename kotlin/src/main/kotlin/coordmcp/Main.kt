package coordmcp

import coordmcp.adapter.outbound.postgres.JooqAdrAllocationAdapter
import coordmcp.adapter.outbound.postgres.JooqAuditAdapter
import coordmcp.adapter.outbound.postgres.JooqDatabase
import coordmcp.adapter.outbound.postgres.JooqWorkItemAdapter
import coordmcp.adapter.outbound.gh.GhIssueTrackerAdapter
import coordmcp.adapter.outbound.git.GitDiffAdapter
import coordmcp.adapter.outbound.gitnexus.GitNexusScopeResolver
import coordmcp.adapter.outbound.graphiti.GraphitiPriorDecisionsAdapter
import coordmcp.application.CheckinKnowledge
import coordmcp.application.CheckinUseCase
import coordmcp.application.CheckoutUseCase
import coordmcp.application.ClaimUseCase
import coordmcp.application.WorkLifecycleUseCase
import coordmcp.mcp.CoordMcpServer
import coordmcp.mcp.CoordPorts
import io.ktor.server.cio.CIO
import io.ktor.server.engine.embeddedServer
import io.modelcontextprotocol.kotlin.sdk.server.StdioServerTransport
import io.modelcontextprotocol.kotlin.sdk.server.mcpStreamableHttp
import java.sql.SQLException
import kotlin.system.exitProcess
import kotlinx.coroutines.awaitCancellation
import kotlinx.io.asSink
import kotlinx.io.asSource
import kotlinx.io.buffered

/** `sysexits.h` EX_CONFIG : configuration invalide. */
public const val EX_CONFIG: Int = 78

/** `sysexits.h` EX_UNAVAILABLE : dépendance indispensable indisponible. */
public const val EX_UNAVAILABLE: Int = 69

/**
 * RACINE DE COMPOSITION — le SEUL endroit autorisé à connaître à la fois les
 * ports et les adaptateurs.
 *
 *     aucun fichier de `mcp/` n'importe un adaptateur sortant, sauf ce fichier-ci.
 *
 * Démarrage FAIL-FAST : une configuration incomplète ne produit ni repli ni
 * connexion approximative. Le processus nomme les variables manquantes et sort
 * en `EX_CONFIG`. Un service qui démarre « quand même » est un service qui
 * échouera plus tard, plus loin, pour une raison moins lisible.
 *
 * `run` est séparé de `main` pour être TESTABLE : on veut prouver les codes de
 * sortie, pas les déclarer. `main` ne fait plus qu'exitProcess(run(...)).
 */
public suspend fun main() {
    // `stdout` appartient au protocole MCP : toute bibliothèque qui y écrit
    // corrompt le flux JSON-RPC. Constaté le 2026-09-13 — le SDK tire
    // `kotlin-logging`, qui imprime une ligne d'initialisation sur stdout avant
    // que Logback ne prenne la main.
    //
    // Plutôt que de lutter contre chaque bibliothèque, on ferme la porte : le
    // VRAI stdout est capturé pour le transport, puis `System.out` est redirigé
    // vers stderr. Tout ce qui écrira « sur stdout » ira sur stderr sans le savoir.
    val protocolOut = System.out
    System.setOut(System.err)

    exitProcess(run(System::getenv, protocolOut))
}

/**
 * Démarre le service. Ne rend la main que pour ÉCHOUER — le chemin nominal est
 * bloquant par construction (`Nothing`), ce qui rend inutile un `return 0`
 * trompeur.
 */
public suspend fun run(env: (String) -> String?, protocolOut: java.io.PrintStream): Int {
    val config = when (val result = CoordConfig.load(env)) {
        is ConfigResult.Missing -> {
            System.err.println("coord-mcp : configuration invalide, démarrage refusé")
            result.violations.forEach { System.err.println("  - $it") }
            System.err.println()
            System.err.println("  Exemple :")
            System.err.println("    export COORD_MCP_JDBC_URL=jdbc:postgresql://127.0.0.1:5432/coord_mcp")
            System.err.println("    export COORD_MCP_DB_USER=$(id -un)")
            System.err.println("    export COORD_MCP_DB_PASSWORD=     # vide si auth trust")
            return EX_CONFIG
        }
        is ConfigResult.Loaded -> result.config
    }

    // Journal de démarrage sur STDERR : sur un serveur MCP stdio, stdout
    // appartient au protocole. Une ligne écrite là corromprait le JSON-RPC.
    // Seul le CANAL du mot de passe apparaît — jamais sa valeur.
    System.err.println(config.describe())
    installShutdownLog()

    val database = try {
        JooqDatabase(config.jdbcUrl, config.user, config.password)
    } catch (e: SQLException) {
        // Base injoignable : ce n'est PAS une erreur de configuration. Deux codes
        // distincts, sinon l'opérateur cherche au mauvais endroit.
        System.err.println("coord-mcp : base injoignable — ${e.message}")
        return EX_UNAVAILABLE
    }

    // Transport choisi explicitement. HTTP est celui du service en production
    // (les agents pointent dessus) ; stdio reste utile pour l'outillage local.
    if (env(TRANSPORT_VAR) == "http") {
        serveHttp(database, config)
    }
    serveStdio(database, config.ciConcurrencyLimit, protocolOut)
}

private val shutdownHookInstalled = java.util.concurrent.atomic.AtomicBoolean(false)

/** Arrêt explicite et traçable, plutôt qu'une disparition silencieuse. */
private fun installShutdownLog() {
    if (!shutdownHookInstalled.compareAndSet(false, true)) return
    Runtime.getRuntime().addShutdownHook(
        Thread { System.err.println("coord-mcp : arrêt demandé, fermeture des ressources") },
    )
}

/**
 * Transport HTTP — celui du service en production.
 *
 * Meme porte que le service Python (8015) : les agents qui pointent dessus
 * n'ont rien a changer, a condition que les NOMS D'OUTILS soient identiques.
 *
 * `start(wait = true)` bloque : le serveur vit jusqu'a l'arret du processus, ce
 * qui est le comportement attendu d'un service, pas d'un outil en ligne de commande.
 */
private suspend fun serveHttp(
    database: JooqDatabase,
    config: CoordConfig,
): Nothing {
    val workItems = JooqWorkItemAdapter(database)
    val adr = JooqAdrAllocationAdapter(database)
    val issueTracker = GhIssueTrackerAdapter()
    val priorDecisions = GraphitiPriorDecisionsAdapter()
    val coord = CoordMcpServer(
        CoordPorts(
            reader = workItems,
            audit = JooqAuditAdapter(database),
            allocations = adr,
            adr = adr,
            lifecycle = WorkLifecycleUseCase(workItems, workItems),
            checkin = CheckinUseCase(
                workItems,
                workItems,
                CheckinKnowledge(GitNexusScopeResolver(), issueTracker, priorDecisions),
                config.ciConcurrencyLimit,
            ),
            checkout = CheckoutUseCase(
                workItems,
                workItems,
                GitDiffAdapter(),
                issueTracker,
                config.ciConcurrencyLimit,
            ),
            claims = ClaimUseCase(workItems, workItems, issueTracker),
            writer = workItems,
            resources = database,
        ),
    )
    System.err.println("coord-mcp : transport HTTP sur 127.0.0.1:${config.httpPort}/mcp")
    embeddedServer(CIO, host = "127.0.0.1", port = config.httpPort) {
        mcpStreamableHttp { coord.server() }
    }.start(wait = true)
    error("embeddedServer a rendu la main alors qu'il devait bloquer")
}

/** Chemin nominal stdio : bloque jusqu'a l'arret. Ne rend jamais la main. */
private suspend fun serveStdio(
    database: JooqDatabase,
    ciLimit: Int,
    protocolOut: java.io.PrintStream,
): Nothing {
    // Un adaptateur par port : un monolithe les servait tous les quatre et
    // grossissait à chaque ajout de registre.
    val workItems = JooqWorkItemAdapter(database)
    val issueTracker = GhIssueTrackerAdapter()
    val priorDecisions = GraphitiPriorDecisionsAdapter()
    val adr = JooqAdrAllocationAdapter(database)
    val coord = CoordMcpServer(
        CoordPorts(
            reader = workItems,
            audit = JooqAuditAdapter(database),
            allocations = adr,
            adr = adr,
            lifecycle = WorkLifecycleUseCase(workItems, workItems),
            checkin = CheckinUseCase(
                workItems,
                workItems,
                CheckinKnowledge(GitNexusScopeResolver(), issueTracker, priorDecisions),
                ciLimit,
            ),
            checkout = CheckoutUseCase(workItems, workItems, GitDiffAdapter(), issueTracker, ciLimit),
            claims = ClaimUseCase(workItems, workItems, issueTracker),
            writer = workItems,
            resources = database,
        ),
    )
    try {
        coord.server().createSession(
            StdioServerTransport(
                System.`in`.asSource().buffered(),
                protocolOut.asSink().buffered(),
            ) { },
        )
        awaitCancellation()
    } finally {
        coord.close()
    }
}

private const val TRANSPORT_VAR = "COORD_MCP_TRANSPORT"
