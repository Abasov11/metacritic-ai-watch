# Проверка резюме отзывов (LLM-as-judge)

Дата прогона: 2026-09-06 17:43 UTC.
Резюме писала модель: `deepseek/deepseek-v4-flash`. Судья: `openai/gpt-4.1-mini`.
Проверено 9 резюме по 7 играм, 77 утверждений, 10 вызовов судьи, $0.011347 (включая контрольный вызов).

## Методика

Каждый пункт `likes`/`dislikes` из резюме отдаётся судье вместе с теми же отзывами, по которым резюме и строилось (до 40 штук, каждый обрезан до 700 символов). Судья отвечает по одному вердикту на пункт.

**Подтверждением считается** отзыв, где та же мысль высказана прямо или очевидным перефразом. Правдоподобность сама по себе, знание об игре извне и догадки не засчитываются.

Судья — модель другого вендора, она не участвует в написании резюме ни как основная, ни как резервная, поэтому никто не проверяет сам себя.

**Ограничения.** Судья — та же технология, что и автор резюме, и ошибается в обе стороны: может не увидеть подтверждение за перефразом или, наоборот, засчитать слишком вольную связь. Выборка маленькая, доверительных интервалов тут нет. Метрика показывает укоренённость в тексте отзывов, а не то, справедливо ли утверждение по отношению к самой игре.

## Контроль судьи

Чтобы стопроцентная доля не оказалась следствием сговорчивого судьи, в тот же прогон подмешиваются заведомо выдуманные утверждения по настоящим отзывам. Исправный судья обязан отвергнуть их все.

Отвергнуто 3 из 3 выдумок.

- отверг: «Игра поддерживает кооператив на восемь игроков по сети» — В отзывах нет упоминаний о кооперативе на восемь игроков по сети.
- отверг: «Отдельно хвалят режим гонок на верблюдах» — В отзывах нет упоминаний о режиме гонок на верблюдах.
- отверг: «Ругают обязательную подписку за 900 рублей в месяц» — В отзывах нет упоминаний об обязательной подписке за 900 рублей в месяц.

## Метрики

| Срез | Подтверждено | Доля |
|---|---|---|
| **Всего** | 77 / 77 | 100.0% |
| Отзывы критиков | 57 / 57 | 100.0% |
| Отзывы игроков | 20 / 20 | 100.0% |
| Пункты «нравится» | 42 / 42 | 100.0% |
| Пункты «не нравится» | 35 / 35 | 100.0% |

## Примеры подтверждённых утверждений

**Onimusha: Way of the Sword** · critic · нравится
> Утверждение: Захватывающая боевая система с акцентом на парирование и технику Issen
>
> Цитата из отзыва: «Onimusha: Way of the Sword marks a triumphant return for Capcom’s samurai action series, combining spectacular visuals with deep, demanding combat built around precise parries, the classic Issen techn»

**Onimusha: Way of the Sword** · critic · нравится
> Утверждение: Эффектные и запоминающиеся битвы с боссами
>
> Цитата из отзыва: «full of boss battles that could make even FromSoftware fans envious»

**Onimusha: Way of the Sword** · critic · нравится
> Утверждение: Харизматичный главный герой Мусаси и интересные персонажи
>
> Цитата из отзыва: «Musashi is a captivating and funny protagonist, accompanied by a cast of equally interesting supporting characters»

**Onimusha: Way of the Sword** · critic · нравится
> Утверждение: Великолепная графика на движке RE Engine и атмосферное воссоздание феодальной Японии
>
> Цитата из отзыва: «Onimusha: Way of the Sword is a visually stunning revival that successfully brings Capcom’s classic franchise into the modern era using the RE Engine»

**Onimusha: Way of the Sword** · critic · нравится
> Утверждение: Успешное возрождение серии с сохранением её духа
>
> Цитата из отзыва: «Onimusha: Way of the Sword masterfully revives the franchise»

**Onimusha: Way of the Sword** · critic · не нравится
> Утверждение: Повторяющиеся под-боссы и ограниченное разнообразие врагов
>
> Цитата из отзыва: «repetitive sub-bosses and a few minor presentation issues do little to diminish an outstanding experience»

**Onimusha: Way of the Sword** · critic · не нравится
> Утверждение: Линейный дизайн уровней и недостаток побочного контента после завершения сюжета
>
> Цитата из отзыва: «it’s a very linear game, and that could be a problem for some... once the story’s finished, there isn’t a breadth of post-game modes»

**Onimusha: Way of the Sword** · critic · не нравится
> Утверждение: Проблемы с камерой в отдельных моментах
>
> Цитата из отзыва: «though the camera can be a little problematic on rare occasions»

**Onimusha: Way of the Sword** · critic · не нравится
> Утверждение: Медленное развитие сюжета в начале и некоторая неровность побочных заданий
>
> Цитата из отзыва: «Its slow narrative buildup and uneven side content hold it back»

**Onimusha: Way of the Sword** · critic · не нравится
> Утверждение: Излишне сложные боссы для некоторых игроков
>
> Цитата из отзыва: «excessively tough bosses... these issues do not diminish the brilliance»

## Неподтверждённые утверждения — все 0

Судья подтвердил каждое утверждение.
