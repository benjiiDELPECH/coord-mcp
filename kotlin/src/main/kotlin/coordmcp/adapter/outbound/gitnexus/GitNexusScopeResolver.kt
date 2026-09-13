package coordmcp.adapter.outbound.gitnexus

import coordmcp.application.port.ScopeExpansion
import coordmcp.application.port.ScopeResolverPort
import java.nio.file.Path
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Adaptateur GitNexus — `gitnexus impact <symbole>` en ligne de commande.
 *
 * Enveloppe la CLI, dont le contrat a été relevé sur le service Python de
 * référence : `gitnexus impact <symbol> -r <alias> --depth N --direction
 * downstream`, réponse JSON avec `target.filePath` et `byDepth.*[].filePath`.
 *
 * TROIS issues distinctes, jamais confondues :
 *   - `Resolved`          le résolveur a répondu ;
 *   - `Resolved(unresolved=…)` réponse PARTIELLE : certains symboles n'ont pas
 *                         été résolus, et on dit lesquels ;
 *   - `Unavailable`       la CLI est absente, ou AUCUN symbole n'a pu être
 *                         résolu : rien n'a été vérifié.
 */
public class GitNexusScopeResolver(
    private val timeoutSeconds: Long = 20,
) : ScopeResolverPort {

    private val json = Json { ignoreUnknownKeys = true }

    private companion object {
        /** Assez pour diagnostiquer, assez court pour un message d'erreur. */
        const val MAX_STDERR_CHARS = 200
    }

    override fun expand(repoPath: String, symbols: List<String>, depth: Int): ScopeExpansion {
        if (symbols.isEmpty()) return ScopeExpansion.notNeeded()

        val alias = Path.of(repoPath).fileName?.toString()
            ?: return ScopeExpansion.Unavailable("chemin de dépôt illisible : '$repoPath'")

        val files = mutableSetOf<String>()
        val unresolved = mutableMapOf<String, String>()

        for (symbol in symbols) {
            when (val result = impact(alias, symbol, depth)) {
                is Impact.Ok -> files += result.files
                is Impact.Error -> unresolved[symbol] = result.reason
            }
        }

        // Tous les symboles en échec : ce n'est pas une réponse partielle, c'est
        // une absence de réponse. La distinction change ce que l'appelant peut en
        // conclure.
        if (unresolved.size == symbols.size) {
            return ScopeExpansion.Unavailable(
                "aucun des ${symbols.size} symboles n'a pu être résolu : " +
                    unresolved.values.first(),
            )
        }
        return ScopeExpansion.Resolved(files, unresolved)
    }

    private sealed interface Impact {
        data class Ok(val files: Set<String>) : Impact
        data class Error(val reason: String) : Impact
    }

    private fun impact(alias: String, symbol: String, depth: Int): Impact = try {
        val process = ProcessBuilder(
            listOf(
                "gitnexus", "impact", symbol,
                "-r", alias,
                "--depth", depth.toString(),
                "--direction", "downstream",
            ),
        ).start()

        val stdout = process.inputStream.bufferedReader().readText()
        val stderr = process.errorStream.bufferedReader().readText()

        if (!process.waitFor(timeoutSeconds, TimeUnit.SECONDS)) {
            process.destroyForcibly()
            Impact.Error("délai de ${timeoutSeconds}s dépassé")
        } else if (stdout.isBlank()) {
            Impact.Error("aucune sortie (${stderr.trim().take(MAX_STDERR_CHARS)})")
        } else {
            parse(stdout)
        }
    } catch (e: java.io.IOException) {
        Impact.Error("gitnexus non exécutable : ${e.message}")
    }

    private fun parse(stdout: String): Impact = try {
        val root = json.parseToJsonElement(stdout).jsonObject

        root["error"]?.jsonPrimitive?.contentOrNull()?.let { return Impact.Error(it) }

        val files = mutableSetOf<String>()
        root["target"]?.jsonObject?.get("filePath")?.jsonPrimitive?.contentOrNull()?.let(files::add)

        root["byDepth"]?.jsonObject?.values?.forEach { bucket ->
            bucket.jsonArrayOrNull()?.forEach { entry ->
                entry.jsonObject["filePath"]?.jsonPrimitive?.contentOrNull()?.let(files::add)
            }
        }
        Impact.Ok(files)
    } catch (_: kotlinx.serialization.SerializationException) {
        Impact.Error("réponse JSON illisible")
    }

    private fun kotlinx.serialization.json.JsonPrimitive.contentOrNull(): String? =
        if (this is kotlinx.serialization.json.JsonNull) null else content

    private fun kotlinx.serialization.json.JsonElement.jsonArrayOrNull():
        List<kotlinx.serialization.json.JsonElement>? = this as? kotlinx.serialization.json.JsonArray
}
