package coordmcp

/**
 * Configuration du processus — un CONTRAT, pas des valeurs par défaut.
 *
 * Chaque règle corrige un défaut réel de la version précédente :
 *
 *  1. Toute variable est OBLIGATOIRE. Un défaut silencieux sur l'URL est pire
 *     qu'une absence : une machine mal configurée se connecte à une autre base
 *     au lieu de refuser de démarrer.
 *
 *  2. Aucun repli sur `user.name`. La version précédente se connectait avec le
 *     rôle de celui qui lançait le processus — sous launchd, ce n'est pas le
 *     même. « Ça marchait dans mon shell » n'est pas une garantie.
 *
 *  3. L'URL est CONTRÔLÉE, pas seulement présente. Une URL non-PostgreSQL
 *     (`jdbc:mysql:…`) passe la présence et échoue à la connexion : on préfère
 *     le dire au démarrage.
 *
 *  4. Le mot de passe vient d'UNE source, jamais de deux. Une valeur en clair
 *     dans l'environnement et un fichier de secret définis simultanément
 *     produisent une ambiguïté silencieuse : on refuse plutôt que de choisir.
 *
 *  5. Le secret n'est JAMAIS journalisé — seul son CANAL l'est.
 */
public data class CoordConfig(
    public val jdbcUrl: String,
    public val ciConcurrencyLimit: Int,
    public val httpPort: Int,
    public val user: String,
    public val password: String,
    public val passwordSource: PasswordSource,
) {
    /**
     * Description opérable, sans secret. Destinée à `stderr` : sur un serveur
     * MCP stdio, `stdout` appartient au protocole — y écrire une ligne de log
     * corromprait le flux JSON-RPC.
     */
    public fun describe(): String = buildString {
        appendLine("coord-mcp : démarrage")
        appendLine("  jdbc        : $jdbcUrl")
        appendLine("  utilisateur : $user")
        append("  mot de passe: ")
        appendLine(
            when (passwordSource) {
                PasswordSource.Environment -> "fourni par l'environnement"
                is PasswordSource.File -> "fourni par fichier (${passwordSource.path})"
            },
        )
        appendLine()
        append("  seuil CI    : $ciConcurrencyLimit travaux simultanés")
    }

    public companion object {
        public const val URL_VAR: String = "COORD_MCP_JDBC_URL"
        public const val USER_VAR: String = "COORD_MCP_DB_USER"
        public const val PASSWORD_VAR: String = "COORD_MCP_DB_PASSWORD"
        public const val PASSWORD_FILE_VAR: String = "COORD_MCP_DB_PASSWORD_FILE"

        /** Seuil du lanceur CI partagé. Valeur par défaut DOCUMENTÉE (doctrine du 26.08.2026). */
        public const val CI_LIMIT_VAR: String = "COORD_MCP_CI_CONCURRENCY_LIMIT"
        public const val DEFAULT_CI_LIMIT: Int = 2

        /** Port du transport HTTP. 8015 = celui du service Python : remplacement a l'identique. */
        public const val PORT_VAR: String = "COORD_MCP_PORT"
        public const val DEFAULT_HTTP_PORT: Int = 8015

        /** Borne haute d'un port TCP valide. */
        private const val MAX_PORT = 65535

        private const val EXPECTED_URL_PREFIX = "jdbc:postgresql:"

        /**
         * `getenv` rend `null` si la variable est ABSENTE — c'est ce qu'on teste.
         * Une variable présente et vide est une valeur délibérée : PostgreSQL en
         * auth `trust` n'exige aucun mot de passe, et il ne faut pas casser le
         * poste de dev tout en refusant une variable oubliée.
         */
        public fun load(
            env: (String) -> String? = System::getenv,
            readFile: (String) -> String? = ::readSecretFile,
        ): ConfigResult {
            val violations = mutableListOf<String>()

            val url = env(URL_VAR)
            when {
                url == null -> violations += "variable d'environnement absente : $URL_VAR"
                url.isBlank() -> violations += "$URL_VAR est vide"
                !url.startsWith(EXPECTED_URL_PREFIX) ->
                    violations += "$URL_VAR doit commencer par '$EXPECTED_URL_PREFIX' (reçu : $url)"
            }

            val user = env(USER_VAR)
            if (user == null) violations += "variable d'environnement absente : $USER_VAR"

            val password = resolvePassword(env, readFile, violations)
            val ciLimit = resolveCiLimit(env, violations)
            val port = env(PORT_VAR)?.toIntOrNull()?.takeIf { it in 1..MAX_PORT }
                ?: DEFAULT_HTTP_PORT

            if (violations.isNotEmpty()) return ConfigResult.Missing(violations)

            return ConfigResult.Loaded(
                CoordConfig(
                    jdbcUrl = url.orEmpty(),
                    user = user.orEmpty(),
                    ciConcurrencyLimit = ciLimit,
                    httpPort = port,
                    password = password.first,
                    passwordSource = password.second,
                ),
            )
        }

        /**
         * Un seuil MAL FORMÉ est une violation, pas un repli silencieux : accepter
         * « abc » et retomber sur 2 ferait croire à une politique appliquée.
         */
        private fun resolveCiLimit(env: (String) -> String?, violations: MutableList<String>): Int {
            val raw = env(CI_LIMIT_VAR) ?: return DEFAULT_CI_LIMIT
            val parsed = raw.toIntOrNull()
            return if (parsed != null && parsed >= 1) {
                parsed
            } else {
                violations += "$CI_LIMIT_VAR doit être un entier >= 1 (reçu '$raw')"
                DEFAULT_CI_LIMIT
            }
        }

        private fun resolvePassword(
            env: (String) -> String?,
            readFile: (String) -> String?,
            violations: MutableList<String>,
        ): Pair<String, PasswordSource> {
            val direct = env(PASSWORD_VAR)
            val filePath = env(PASSWORD_FILE_VAR)

            return when {
                direct != null && filePath != null -> {
                    violations += "ambiguïté : $PASSWORD_VAR ET $PASSWORD_FILE_VAR sont définis — " +
                        "n'en garder qu'un, on ne choisit pas à votre place"
                    "" to PasswordSource.Environment
                }
                direct != null -> direct to PasswordSource.Environment
                filePath != null -> {
                    // Le `trim` est ICI, pas dans le lecteur : un secret de
                    // fichier se termine par un saut de ligne, et la
                    // normalisation appartient au contrat de configuration. La
                    // laisser dans l'implémentation d'I/O la rendrait
                    // contournable par tout autre lecteur.
                    val content = readFile(filePath)?.trim()
                    if (content == null) {
                        violations += "$PASSWORD_FILE_VAR pointe sur un fichier illisible : $filePath"
                        "" to PasswordSource.File(filePath)
                    } else {
                        content to PasswordSource.File(filePath)
                    }
                }
                else -> {
                    violations += "mot de passe absent : définir $PASSWORD_VAR ou $PASSWORD_FILE_VAR"
                    "" to PasswordSource.Environment
                }
            }
        }

        /** Lecture brute — la normalisation du contenu est faite par le contrat, pas ici. */
        private fun readSecretFile(path: String): String? = try {
            java.nio.file.Files.readString(java.nio.file.Path.of(path))
        } catch (_: java.io.IOException) {
            null
        }
    }
}

/** D'où vient le mot de passe. Journalisé ; la valeur ne l'est jamais. */
public sealed interface PasswordSource {
    public data object Environment : PasswordSource

    public data class File(public val path: String) : PasswordSource
}

public sealed interface ConfigResult {
    public data class Loaded(public val config: CoordConfig) : ConfigResult

    /** Toutes les violations d'un coup : on ne fait pas redémarrer l'opérateur trois fois. */
    public data class Missing(public val violations: List<String>) : ConfigResult
}
