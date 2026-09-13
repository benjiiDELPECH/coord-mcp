package coordmcp.adapter.outbound.gh

import coordmcp.application.port.BodyOutcome
import coordmcp.application.port.IssueOutcome
import coordmcp.application.port.IssueRef
import coordmcp.application.port.IssueSearchOutcome
import coordmcp.application.port.IssueTrackerPort
import coordmcp.application.port.NewIssueRequest
import coordmcp.application.port.PullRequestOutcome
import coordmcp.domain.PullRequestRef
import java.nio.file.Path
import java.util.concurrent.TimeUnit

/**
 * Adaptateur `gh` — parle au suivi d'issues en ligne de commande.
 *
 * Deux règles non négociables :
 *  1. **Aucun échec n'est avalé.** `gh` absent, non authentifié, jalon inexistant :
 *     chaque cas produit un `Failed` nommé. Un `Ok` signifie qu'une issue EXISTE
 *     et que son numéro a été lu dans la sortie de la commande.
 *  2. **Délai borné.** Une commande réseau qui ne rend jamais la main bloquerait
 *     un agent indéfiniment ; on la tue et on le dit.
 */
public class GhIssueTrackerAdapter(
    private val timeoutSeconds: Long = 30,
) : IssueTrackerPort {

    override fun assignToMe(repoPath: String, issueNumber: Int): IssueOutcome {
        val slug = resolveSlug(repoPath)
            ?: return IssueOutcome.Failed("remote illisible pour '$repoPath' — dépôt sans origine ?")

        val result = run(listOf("gh", "issue", "edit", issueNumber.toString(), "--repo", slug, "--add-assignee", "@me"))
        return when (result) {
            is CommandResult.Ok -> IssueOutcome.Ok(issueNumber)
            is CommandResult.Error -> IssueOutcome.Failed("gh issue edit : ${result.detail}")
        }
    }

    override fun issueBody(repoPath: String, issueNumber: Int): BodyOutcome {
        val slug = resolveSlug(repoPath)
            ?: return BodyOutcome.Unavailable("remote illisible pour '$repoPath'")

        val args = listOf("gh", "issue", "view", issueNumber.toString(), "--repo", slug, "--json", "body")
        return when (val result = run(args)) {
            is CommandResult.Error -> BodyOutcome.Unavailable(result.detail)
            is CommandResult.Ok -> BodyOutcome.Fetched(GhJson.string(result.stdout, "body").orEmpty())
        }
    }

    override fun searchOpenIssues(repoPath: String, query: String, limit: Int): IssueSearchOutcome {
        val slug = resolveSlug(repoPath)
            ?: return IssueSearchOutcome.Unavailable("remote illisible pour '$repoPath'")

        val args = listOf(
            "gh", "issue", "list", "--repo", slug, "--state", "open",
            "--search", query, "--limit", limit.toString(), "--json", "number,title,url",
        )
        return when (val result = run(args)) {
            is CommandResult.Error -> IssueSearchOutcome.Unavailable(result.detail)
            is CommandResult.Ok -> IssueSearchOutcome.Found(GhJson.issues(result.stdout))
        }
    }

    override fun openPullRequests(repoPath: String): PullRequestOutcome {
        val slug = resolveSlug(repoPath)
            ?: return PullRequestOutcome.Unavailable("remote illisible pour '$repoPath'")

        val args = listOf(
            "gh", "pr", "list", "--repo", slug, "--state", "open", "--limit", PR_LIMIT.toString(),
            "--json", "number,title,url,files",
        )
        return when (val result = run(args)) {
            is CommandResult.Error -> PullRequestOutcome.Unavailable(result.detail)
            is CommandResult.Ok -> PullRequestOutcome.Fetched(GhJson.pullRequests(result.stdout))
        }
    }

    override fun createIssue(request: NewIssueRequest): IssueOutcome {
        val slug = resolveSlug(request.repoPath)
            ?: return IssueOutcome.Failed("remote illisible pour '${request.repoPath}'")

        val args = mutableListOf(
            "gh", "issue", "create",
            "--repo", slug,
            "--title", request.title,
            "--body", request.body,
        )
        request.labels.filter { it.isNotBlank() }.forEach { args += listOf("--label", it) }

        request.milestoneNumber?.let { number ->
            // `gh issue create --milestone` attend le TITRE, pas le numéro : on le
            // résout explicitement. Un jalon introuvable est un échec, pas un
            // jalon silencieusement omis.
            val title = milestoneTitle(slug, number)
                ?: return IssueOutcome.Failed("jalon $number introuvable sur $slug")
            args += listOf("--milestone", title)
        }

        return when (val result = run(args)) {
            is CommandResult.Ok -> when (val number = parseIssueNumber(result.stdout)) {
                null -> IssueOutcome.Failed(
                    "issue créée mais numéro illisible dans : ${result.stdout.trim()}",
                )
                else -> IssueOutcome.Ok(number)
            }
            is CommandResult.Error -> IssueOutcome.Failed("gh issue create : ${result.detail}")
        }
    }

    /** `gh issue create` rend l'URL de l'issue ; le numéro en est le dernier segment. */
    private fun parseIssueNumber(stdout: String): Int? =
        stdout.trim().lineSequence().lastOrNull()?.trim()?.substringAfterLast('/')?.toIntOrNull()

    private fun milestoneTitle(slug: String, number: Int): String? =
        when (val result = run(listOf("gh", "api", "repos/$slug/milestones/$number", "--jq", ".title"))) {
            is CommandResult.Ok -> result.stdout.trim().takeIf { it.isNotEmpty() && it != "null" }
            is CommandResult.Error -> null
        }

    private fun resolveSlug(repoPath: String): String? {
        val directory = Path.of(repoPath).toFile()
        if (!directory.isDirectory) return null

        return when (val result = run(listOf("git", "-C", repoPath, "remote", "get-url", "origin"))) {
            is CommandResult.Ok -> slugOfRemote(result.stdout.trim())
            is CommandResult.Error -> null
        }
    }

    /** Accepte `git@host:owner/repo.git` et `https://host/owner/repo.git`. */
    internal fun slugOfRemote(remote: String): String? {
        val withoutSuffix = remote.removeSuffix(".git")
        val path = when {
            withoutSuffix.contains(':') && withoutSuffix.startsWith("git@") ->
                withoutSuffix.substringAfter(':')
            else -> withoutSuffix.substringAfter("://", withoutSuffix).substringAfter('/', "")
        }
        val parts = path.split('/').filter { it.isNotBlank() }
        return if (parts.size >= 2) parts.takeLast(2).joinToString("/") else null
    }

    private companion object {
        /** Borne le nombre de PR examinées : croiser 200 PR noierait le rapport. */
        const val PR_LIMIT = 30
    }

    private sealed interface CommandResult {
        data class Ok(val stdout: String) : CommandResult
        data class Error(val detail: String) : CommandResult
    }

    private fun run(args: List<String>): CommandResult = try {
        val process = ProcessBuilder(args).redirectErrorStream(false).start()
        val stdout = process.inputStream.bufferedReader().readText()
        val stderr = process.errorStream.bufferedReader().readText()

        if (!process.waitFor(timeoutSeconds, TimeUnit.SECONDS)) {
            process.destroyForcibly()
            CommandResult.Error("délai de ${timeoutSeconds}s dépassé")
        } else if (process.exitValue() != 0) {
            CommandResult.Error(stderr.trim().ifEmpty { "code ${process.exitValue()}" })
        } else {
            CommandResult.Ok(stdout)
        }
    } catch (e: java.io.IOException) {
        // `gh` absent, ou inexécutable : on le nomme au lieu de rendre un vide.
        CommandResult.Error("commande non exécutable : ${e.message}")
    }
}
