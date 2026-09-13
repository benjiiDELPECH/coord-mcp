package coordmcp

import coordmcp.domain.AdrNumbering
import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * Règles de nommage des ADR — reproduites du service Python.
 *
 * La parité n'est pas cosmétique : deux slugifications divergentes produiraient
 * deux fichiers différents pour un même numéro, et le disque deviendrait
 * incohérent avec la base. Ces valeurs sont donc figées contre l'implémentation
 * Python de référence.
 */
class AdrNumberingTest {

    @Test
    fun `slugify produit du kebab-case ascii`() {
        assertEquals("corriger-la-capture-projectmem", AdrNumbering.slugify("Corriger la capture ProjectMem"))
        assertEquals("decision-d-architecture", AdrNumbering.slugify("Décision d'architecture"))
        assertEquals("accentues-et-cedilles", AdrNumbering.slugify("Accentués et cédillés"))
    }

    @Test
    fun `slugify ne rend jamais une chaine vide`() {
        assertEquals("untitled", AdrNumbering.slugify(""))
        assertEquals("untitled", AdrNumbering.slugify("!!! ???"))
        assertEquals("untitled", AdrNumbering.slugify("---"))
    }

    @Test
    fun `le nom de fichier est zero-padde sur trois chiffres`() {
        assertEquals("ADR-007-petit-numero.md", AdrNumbering.filename(7, "petit-numero"))
        assertEquals("ADR-225-capex-monte-carlo.md", AdrNumbering.filename(225, "capex-monte-carlo"))
        // Au-delà de 999 la largeur s'étend : `%03d` est un minimum, pas un maximum.
        assertEquals("ADR-1024-grand.md", AdrNumbering.filename(1024, "grand"))
    }

    @Test
    fun `le candidat tient compte du disque ET de la base`() {
        // Le cas qui compte : un ADR écrit à la main sur le disque, absent de la
        // base. L'ignorer allouerait un numéro déjà pris par un FICHIER.
        assertEquals(226, AdrNumbering.nextCandidate(maxOnDisk = 225, maxInDatabase = 224))
        assertEquals(226, AdrNumbering.nextCandidate(maxOnDisk = 224, maxInDatabase = 225))
        assertEquals(1, AdrNumbering.nextCandidate(maxOnDisk = 0, maxInDatabase = 0))
    }

    @Test
    fun `un fichier hors convention ne compte pas comme un numero`() {
        assertEquals(225, AdrNumbering.numberFromFilename("ADR-225-capex.md"))
        assertEquals(7, AdrNumbering.numberFromFilename("ADR-007-x.md"))
        assertEquals(0, AdrNumbering.numberFromFilename("README.md"))
        assertEquals(0, AdrNumbering.numberFromFilename("ADR-capex.md"))
    }
}
