package coordmcp.domain

import java.text.Normalizer

/**
 * Règles de numérotation des ADR — PURES, sans accès disque ni base.
 *
 * Le SCAN (fichiers sur disque, allocations en base) est de l'I/O : il vit dans
 * l'adaptateur. Ce qui est ici est la règle, reproduite à l'identique du service
 * Python pour que les deux implémentations produisent le MÊME nom de fichier —
 * une divergence ferait écrire deux ADR différentes pour un même numéro.
 */
public object AdrNumbering {

    private val ADR_FILE = Regex("^ADR-(\\d{3,4})-")
    private val NON_ASCII = Regex("[^\\p{ASCII}]")
    private val NON_ALNUM = Regex("[^a-z0-9]+")

    /**
     * kebab-case ASCII, conservateur. Reproduit `slugify` du Python :
     * NFKD -> ASCII, minuscules, tout caractère non alphanumérique devient un
     * tiret, tirets de bord retirés, repli `untitled`.
     */
    public fun slugify(text: String): String {
        val ascii = Normalizer.normalize(text, Normalizer.Form.NFKD).replace(NON_ASCII, "")
        val slug = ascii.lowercase().replace(NON_ALNUM, "-").trim('-')
        return slug.ifEmpty { "untitled" }
    }

    /** Largeur minimale du numéro. Au-delà de 999 elle s'étend : `%03d` est un minimum, pas un maximum. */
    private const val ADR_WIDTH = 3

    /** `ADR-%03d-slug.md` — même largeur que le Python (`%03d`). */
    public fun filename(number: Int, slug: String): String =
        "ADR-" + number.toString().padStart(ADR_WIDTH, '0') + "-" + slug + ".md"

    /**
     * Numéro candidat : le maximum des DEUX sources, plus un.
     *
     * Le disque compte autant que la base : un ADR écrit à la main n'est pas
     * dans `adr_allocations`, et l'ignorer produirait une collision de fichier —
     * le numéro serait « libre » en base et déjà pris sur le disque.
     */
    public fun nextCandidate(maxOnDisk: Int, maxInDatabase: Int): Int =
        maxOf(maxOnDisk, maxInDatabase) + 1

    /** Numéro extrait d'un nom de fichier, ou 0 si le fichier ne suit pas la convention. */
    public fun numberFromFilename(fileName: String): Int =
        ADR_FILE.find(fileName)?.groupValues?.get(1)?.toIntOrNull() ?: 0
}
