-- 0019 · La búsqueda como conversación (NEX-611; Moibe, 2026-11-26: «va, dale al 611»).
--
-- Cada pregunta de la búsqueda (un renglón de `searchQuestions`, NEX-609) pertenece ahora a una CONVERSACIÓN: la pregunta nueva se entiende con las
-- anteriores de esa misma conversación («¿y cuántas de esas tienen campos corregidos?»). Al modelo solo le llegan las preguntas anteriores, cómo se
-- entendieron y sus consultas, que se leen de esta misma bitácora filtrando por organización Y por persona: una conversación nunca mezcla organizaciones
-- ni personas, aunque alguien mande el id de otra.
--
--   conversationId   `conversacion-` + 32 hexadecimales; lo crea el back con la primera pregunta y la pantalla lo devuelve con las siguientes.
--   turn             el número de la pregunta dentro de su conversación (1, 2, 3…). Cuentan todas, también las que fallaron.
--   usesContext      el modelo dijo que tomó algo de las preguntas anteriores (1), o que la contestó sola (0); NULL si no había anteriores o si la
--                    consulta se reusó. Una respuesta que depende de la conversación NO se reusa para la misma pregunta suelta, no aparece en «Tus
--                    preguntas recientes» y no se propone como ejemplo: sin su conversación, la misma pregunta quiere decir otra cosa.
--   suggestionsJson  hasta tres preguntas para seguir, que escribió el modelo sin ver los datos (`["¿Y por tipo documental?", …]`). Se guardan para
--                    que una respuesta reusada también las traiga.
--
-- Los renglones de las respuestas siguen SIN guardarse. Las columnas nacen vacías: lo anterior a esta migración no tiene conversación.

IF OBJECT_ID('nexus.searchQuestions', 'U') IS NULL
    THROW 51019, 'Falta nexus.searchQuestions: la crea la migración 0018.', 1;
GO

ALTER TABLE [nexus].[searchQuestions] ADD
    [conversationId] varchar(45) NULL,
    [turn] smallint NULL,
    [usesContext] bit NULL,
    [suggestionsJson] nvarchar(2000) NULL;
GO

ALTER TABLE [nexus].[searchQuestions] ADD
    -- Una pregunta con conversación trae su número, y el id tiene la forma de siempre (en minúsculas: la comparación de la base no distingue).
    CONSTRAINT [CK_searchQuestions_conversation] CHECK (
        ([conversationId] IS NULL AND [turn] IS NULL)
        OR ([conversationId] IS NOT NULL AND [turn] IS NOT NULL AND [turn] >= (1)
            AND [conversationId] LIKE 'conversacion-%' AND [conversationId] COLLATE Latin1_General_BIN2 = lower([conversationId])
            AND len([conversationId]) = (45) AND NOT substring([conversationId], 14, 32) LIKE '%[^0-9a-f]%')
    ),
    -- Tomar algo de las anteriores solo se puede desde la segunda pregunta, y solo en una respuesta que salió bien.
    CONSTRAINT [CK_searchQuestions_usesContext] CHECK ([usesContext] IS NULL OR ([turn] IS NOT NULL AND [outcome] = 'ok' AND ([usesContext] = (0) OR [turn] > (1)))),
    CONSTRAINT [CK_searchQuestions_suggestions] CHECK ([suggestionsJson] IS NULL OR isjson([suggestionsJson]) = (1));
GO

-- Las preguntas anteriores de una conversación se leen por organización, persona y conversación, en orden.
CREATE NONCLUSTERED INDEX [IX_searchQuestions_conversation] ON [nexus].[searchQuestions] ([tenant], [userGuid], [conversationId], [questionId]) INCLUDE ([outcome], [turn]);
GO
