package coordmcp

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlinx.coroutines.test.runTest

/**
 * Le contrat de DÉMARRAGE, prouvé par le comportement — pas par une déclaration.
 *
 * Un ingénieur SRE ne se contente pas qu'un service « refuse une config
 * invalide » : il veut savoir avec QUEL code de sortie, pour que la supervision
 * distingue une faute de configuration d'une dépendance en panne. Ce sont deux
 * diagnostics opposés : l'un demande de corriger des variables, l'autre de
 * réveiller une base.
 */
class StartupContractTest {

    private fun env(vararg pairs: Pair<String, String?>): (String) -> String? {
        val map = pairs.toMap()
        return { key -> map[key] }
    }

    @Test
    fun `configuration absente donne EX_CONFIG et non une connexion hasardeuse`() = runTest {
        assertEquals(EX_CONFIG, run(env(), System.out), "aucune variable fournie doit sortir en EX_CONFIG (78)")
    }

    @Test
    fun `base injoignable donne EX_UNAVAILABLE, distinct de la config`() = runTest {
        // Port 1 sur la boucle locale : refus immédiat, pas de délai d'attente.
        val code = run(
            env(
                CoordConfig.URL_VAR to "jdbc:postgresql://127.0.0.1:1/coord_mcp",
                CoordConfig.USER_VAR to "personne",
                CoordConfig.PASSWORD_VAR to "",
            ),
            System.out,
        )
        assertEquals(
            EX_UNAVAILABLE,
            code,
            "une base injoignable n'est PAS une faute de configuration : code 69, pas 78",
        )
    }

    @Test
    fun `les deux codes sont distincts, sinon le diagnostic est impossible`() {
        assertEquals(false, EX_CONFIG == EX_UNAVAILABLE)
    }
}
