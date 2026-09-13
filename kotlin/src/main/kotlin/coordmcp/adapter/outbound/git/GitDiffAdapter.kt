package coordmcp.adapter.outbound.git

import coordmcp.application.port.DiffPort
import coordmcp.application.port.DiffRequest
import coordmcp.application.port.DiffResult
import coordmcp.application.port.DiffSource
import java.util.concurrent.TimeUnit

/**
 * Adaptateur git — diff contre la branche de référence.
 *
 * Le nom du remote de référence n'est PAS codé en dur. `coord-mcp` sert
 * plusieurs dépôts, et leur remote canonique diffère : `alert-immo` a `forgejo`
 * comme canonical, avec `origin` resté en miroir GitHub périmé. Differ contre le
 * mauvais remote produit des centaines de faux fichiers « en conflit » — un nom
 * de remote figé est structurellement faux dans un outil multi-dépôts.
 *
 * Résolution : `coord-mcp.canonical-remote` (git config) → `COORD_MCP_REFERENCE_REMOTE`
 * (environnement) → `origin` en dernier recours.
 *
 * Limite assumée : la cascade multi-worktree du service Python (worktree dont le
 * diff recouvre le mieux le périmètre déclaré, et détection d'ambiguïté quand
 * plusieurs sont à égalité) n'est PAS portée. Ici `worktree_path` est explicite ;
 * il n'y a pas de devinette.
 */
public class GitDiffAdapter(
    private val env: (String) -> String? = System::getenv,
    private val timeoutSeconds: Long = 15,
) : DiffPort {

    override fun changedFiles(request: DiffRequest): DiffResult {
        request.explicitFiles?.let { explicit ->
            return DiffResult.Resolved(explicit.toSet(), DiffSource.EXPLICIT)
        }

        val target = request.worktreePath ?: request.repoPath
        val source = if (request.worktreePath != null) DiffSource.WORKTREE_OVERRIDE else DiffSource.REPO_HEAD

        val files = diffAgainstReference(target)
            ?: return DiffResult.Unavailable(
                "diff impossible pour '$target' — dépôt sans remote, référence absente, ou délai dépassé",
            )

        val warnings = if (files.isEmpty()) {
            listOf(
                "diff vide : HEAD est peut-être déjà sur la branche de référence, ou rien n'est " +
                    "modifié. Passer `diff_files` explicitement si des changements sont attendus.",
            )
        } else {
            emptyList()
        }
        return DiffResult.Resolved(files, source, warnings)
    }

    /** Rend `null` dès que la référence ou le diff ne peut pas être établi. */
    private fun diffAgainstReference(target: String): Set<String>? {
        val reference = referenceRef(target) ?: return null
        val base = git(target, listOf("merge-base", "HEAD", reference)) ?: return null
        val output = git(target, listOf("diff", "--name-only", base)) ?: return null
        return output.lineSequence().map { it.trim() }.filter { it.isNotEmpty() }.toSet()
    }

    /** `env` prime sur la config git : l'environnement est plus facile à surcharger ponctuellement. */
    private fun referenceRef(repoPath: String): String? {
        val remote = env(REFERENCE_REMOTE_VAR)
            ?: git(repoPath, listOf("config", "--get", CANONICAL_REMOTE_KEY))?.trim()
            ?: DEFAULT_REMOTE

        for (candidate in listOf("$remote/main", "$remote/master", "HEAD~1")) {
            if (git(repoPath, listOf("rev-parse", "--verify", "--quiet", candidate)) != null) {
                return candidate
            }
        }
        return null
    }

    /** Rend `null` sur tout échec : l'appelant en fait une indisponibilité nommée. */
    private fun git(repoPath: String, args: List<String>): String? = try {
        val process = ProcessBuilder(listOf("git", "-C", repoPath) + args).start()
        val stdout = process.inputStream.bufferedReader().readText()
        process.errorStream.bufferedReader().readText()

        if (!process.waitFor(timeoutSeconds, TimeUnit.SECONDS)) {
            process.destroyForcibly()
            null
        } else if (process.exitValue() != 0) {
            null
        } else {
            stdout
        }
    } catch (_: java.io.IOException) {
        null
    }

    private companion object {
        const val CANONICAL_REMOTE_KEY = "coord-mcp.canonical-remote"
        const val REFERENCE_REMOTE_VAR = "COORD_MCP_REFERENCE_REMOTE"
        const val DEFAULT_REMOTE = "origin"
    }
}
