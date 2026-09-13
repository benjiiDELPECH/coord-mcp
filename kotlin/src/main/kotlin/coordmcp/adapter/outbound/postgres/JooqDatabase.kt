package coordmcp.adapter.outbound.postgres

import java.sql.Connection
import java.sql.DriverManager
import org.jooq.DSLContext
import org.jooq.SQLDialect
import org.jooq.impl.DSL

/**
 * Connexion jOOQ partagée par les adaptateurs PostgreSQL.
 *
 * Une seule connexion, un seul cycle de vie : les adaptateurs ne possèdent pas
 * la ressource, ils l'empruntent. Sinon chaque adaptateur ouvrirait la sienne et
 * la fermeture deviendrait un jeu de dominos.
 *
 * Local et mono-écrivain : une connexion persistante suffit et évite un pool
 * dont on n'a pas besoin. Le jour où plusieurs processus écriront, c'est ce
 * fichier — et lui seul — qu'il faudra remplacer par un pool.
 */
public class JooqDatabase(
    jdbcUrl: String,
    user: String,
    password: String = "",
) : AutoCloseable {

    private val connection: Connection = DriverManager.getConnection(jdbcUrl, user, password)

    internal val dsl: DSLContext = DSL.using(connection, SQLDialect.POSTGRES)

    override fun close() {
        connection.close()
    }
}
