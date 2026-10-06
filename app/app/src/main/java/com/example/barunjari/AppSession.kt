package com.example.barunjari

import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone

internal enum class MeasurementStatus { IDLE, RUNNING, PAUSED }

internal fun measurementElapsed(baseMillis: Long, startedAt: Long, now: Long): Long =
    baseMillis + (now - startedAt).coerceAtLeast(0L)

internal fun measurementTime(millis: Long): String {
    val seconds = millis / 1000
    return String.format(Locale.ROOT, "%02d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
}

/** DatePicker uses UTC midnight. Treat these values as calendar dates, not local instants. */
internal object ReportDates {
    const val DAY_MILLIS = 86_400_000L
    private val utc = TimeZone.getTimeZone("UTC")

    fun today(): Long {
        val local = Calendar.getInstance(TimeZone.getTimeZone("Asia/Seoul"))
        return Calendar.getInstance(utc).apply {
            clear()
            set(local.get(Calendar.YEAR), local.get(Calendar.MONTH), local.get(Calendar.DAY_OF_MONTH))
        }.timeInMillis
    }

    fun shift(date: Long, days: Int): Long = date + days * DAY_MILLIS
    fun weekEnding(date: Long): List<Long> = (6 downTo 0).map { shift(date, -it) }
    fun calendarWeek(date: Long): List<Long> {
        val day = Calendar.getInstance(utc).apply { timeInMillis = date }.get(Calendar.DAY_OF_WEEK)
        val monday = shift(date, -((day + 5) % 7))
        return (0..6).map { shift(monday, it) }
    }
    fun monthStart(date: Long): Long = Calendar.getInstance(utc).apply {
        timeInMillis = date
        set(Calendar.DAY_OF_MONTH, 1)
    }.timeInMillis
    fun shiftMonth(date: Long, months: Int): Long = Calendar.getInstance(utc).apply {
        timeInMillis = monthStart(date)
        add(Calendar.MONTH, months)
    }.timeInMillis
    fun monthCells(date: Long): List<Long?> {
        val calendar = Calendar.getInstance(utc).apply { timeInMillis = monthStart(date) }
        val offset = calendar.get(Calendar.DAY_OF_WEEK) - Calendar.SUNDAY
        val count = calendar.getActualMaximum(Calendar.DAY_OF_MONTH)
        val cells = ((offset + count + 6) / 7) * 7
        return (0 until cells).map { if (it in offset until offset + count) shift(calendar.timeInMillis, it - offset) else null }
    }
    fun format(date: Long, pattern: String): String =
        SimpleDateFormat(pattern, Locale.KOREAN).apply { timeZone = utc }.format(Date(date))

    fun label(date: Long, weekly: Boolean): String = if (weekly) {
        val days = calendarWeek(date)
        "${format(days.first(), "yyyy.M.d")} – ${format(days.last(), "yyyy.M.d")}"
    } else {
        format(date, "yyyy년 M월 d일 (E)")
    }
}
