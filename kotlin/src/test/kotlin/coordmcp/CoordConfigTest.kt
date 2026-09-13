package coordmcp

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

/**
 * Le contrat de configuration.
 *
 * Ces tests existent parce que la version précédente se connectait avec des
 * valeurs par défaut silencieuses — dont `user.name`. Un test qui échoue au
 * démarrage vaut mieux qu'un service qui démarre sur la mauvaise base.
 */
class CoordConfigTest {

    private fun env(vararg pairs: Pair<String, String?>): (String) -> String? {
        val map = pairs.toMap()
        return { key -> map[key] }
    }

    @Test
    fun `toutes les variables absentes, les trois sont nommees`() {
        val result = CoordConfig.load(env())

        val missing = assertIs<ConfigResult.Missing>(result)
        assertEquals(3, missing.violations.size, "on signale TOUT d'un coup, pas une par redémarrage")
        listOf(CoordConfig.URL_VAR, CoordConfig.USER_VAR, CoordConfig.PASSWORD_VAR).forEach { name ->
            assertTrue(
                missing.violations.any { it.contains(name) },
                "la violation doit nommer $name",
            )
        }
    }

    @Test
    fun `une seule variable manquante est nommee, pas les autres`() {
        val result = CoordConfig.load(
            env(
                CoordConfig.URL_VAR to "jdbc:postgresql://127.0.0.1:5432/coord_mcp",
                CoordConfig.USER_VAR to "someone",
                CoordConfig.PASSWORD_VAR to null,
            ),
        )

        val missing = assertIs<ConfigResult.Missing>(result)
        assertEquals(1, missing.violations.size)
        assertTrue(missing.violations.single().contains(CoordConfig.PASSWORD_VAR))
    }

    @Test
    fun `presente mais vide est une valeur deliberee, pas une absence`() {
        // PostgreSQL en auth `trust` n'exige aucun mot de passe : exiger la
        // PRESENCE sans exiger la non-vacuité permet de ne pas casser le poste
        // de dev, tout en refusant une variable oubliée.
        val result = CoordConfig.load(
            env(
                CoordConfig.URL_VAR to "jdbc:postgresql://127.0.0.1:5432/coord_mcp",
                CoordConfig.USER_VAR to "someone",
                CoordConfig.PASSWORD_VAR to "",
            ),
        )

        val loaded = assertIs<ConfigResult.Loaded>(result)
        assertEquals("", loaded.config.password)
        assertEquals("someone", loaded.config.user)
    }

    @Test
    fun `une URL non PostgreSQL est refusee au demarrage, pas a la connexion`() {
        val result = CoordConfig.load(
            env(
                CoordConfig.URL_VAR to "jdbc:mysql://127.0.0.1:3306/coord_mcp",
                CoordConfig.USER_VAR to "someone",
                CoordConfig.PASSWORD_VAR to "",
            ),
        )

        val missing = assertIs<ConfigResult.Missing>(result)
        assertTrue(missing.violations.single().contains("jdbc:postgresql:"))
    }

    @Test
    fun `deux sources de mot de passe font echouer, on ne choisit pas a la place de l'operateur`() {
        val result = CoordConfig.load(
            env(
                CoordConfig.URL_VAR to "jdbc:postgresql://127.0.0.1:5432/coord_mcp",
                CoordConfig.USER_VAR to "someone",
                CoordConfig.PASSWORD_VAR to "en-clair",
                CoordConfig.PASSWORD_FILE_VAR to "/run/secrets/coord_db",
            ),
        )

        val missing = assertIs<ConfigResult.Missing>(result)
        assertTrue(missing.violations.single().contains("ambiguïté"))
    }

    @Test
    fun `le mot de passe peut venir d'un fichier, saut de ligne retire`() {
        val result = CoordConfig.load(
            env(
                CoordConfig.URL_VAR to "jdbc:postgresql://127.0.0.1:5432/coord_mcp",
                CoordConfig.USER_VAR to "someone",
                CoordConfig.PASSWORD_FILE_VAR to "/run/secrets/coord_db",
            ),
            readFile = { "s3cr3t\n" },
        )

        val loaded = assertIs<ConfigResult.Loaded>(result)
        assertEquals("s3cr3t", loaded.config.password)
        assertEquals(PasswordSource.File("/run/secrets/coord_db"), loaded.config.passwordSource)
    }

    @Test
    fun `un fichier de secret illisible est une violation, pas un mot de passe vide`() {
        val result = CoordConfig.load(
            env(
                CoordConfig.URL_VAR to "jdbc:postgresql://127.0.0.1:5432/coord_mcp",
                CoordConfig.USER_VAR to "someone",
                CoordConfig.PASSWORD_FILE_VAR to "/run/secrets/absent",
            ),
            readFile = { null },
        )

        val missing = assertIs<ConfigResult.Missing>(result)
        assertTrue(missing.violations.single().contains("illisible"))
    }

    @Test
    fun `le journal de demarrage ne contient JAMAIS le mot de passe`() {
        val loaded = assertIs<ConfigResult.Loaded>(
            CoordConfig.load(
                env(
                    CoordConfig.URL_VAR to "jdbc:postgresql://127.0.0.1:5432/coord_mcp",
                    CoordConfig.USER_VAR to "someone",
                    CoordConfig.PASSWORD_VAR to "mot-de-passe-tres-secret",
                ),
            ),
        )

        val description = loaded.config.describe()
        assertEquals(false, description.contains("mot-de-passe-tres-secret"))
        assertTrue(description.contains("environnement"), "seul le CANAL est journalisé")
    }

    @Test
    fun `aucune valeur de repli, user vient de l'environnement et jamais de user_name`() {
        val loaded = assertIs<ConfigResult.Loaded>(
            CoordConfig.load(
                env(
                    CoordConfig.URL_VAR to "jdbc:postgresql://db.example:5432/coord_mcp",
                    CoordConfig.USER_VAR to "coord_service",
                    CoordConfig.PASSWORD_VAR to "secret",
                ),
            ),
        )
        // Sous launchd, `user.name` n'est pas celui du shell : s'y replier
        // connecterait le service avec le mauvais rôle, sans aucun signe.
        assertEquals("coord_service", loaded.config.user)
    }
}
