package com.example.barunjari

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

class AppSessionTest {
    private fun date(value: String): Long = SimpleDateFormat("yyyy-MM-dd", Locale.ROOT).apply {
        timeZone = TimeZone.getTimeZone("UTC")
    }.parse(value)!!.time

    @Test
    fun calendarGridIncludesLeapDayAndSundayLeadingSpaces() {
        val cells = ReportDates.monthCells(date("2028-02-15"))
        assertEquals(35, cells.size)
        assertEquals(2, cells.takeWhile { it == null }.size)
        assertEquals(29, cells.filterNotNull().size)
        assertEquals(date("2028-02-01"), cells.filterNotNull().first())
        assertEquals(date("2028-02-29"), cells.filterNotNull().last())
    }

    @Test
    fun monthNavigationClampsToFirstDayAcrossYearBoundary() {
        assertEquals(date("2027-01-01"), ReportDates.shiftMonth(date("2026-12-31"), 1))
        assertEquals(date("2026-12-01"), ReportDates.shiftMonth(date("2027-01-31"), -1))
    }

    @Test
    fun weeklyRangeIncludesExactlySevenDatesAcrossYearBoundary() {
        val end = date("2027-01-03")
        val dates = ReportDates.weekEnding(end)
        assertEquals(7, dates.size)
        assertEquals(date("2026-12-28"), dates.first())
        assertEquals(end, dates.last())
        assertTrue(dates.zipWithNext().all { (a, b) -> b - a == ReportDates.DAY_MILLIS })
        assertEquals("2026.12.28 – 2027.1.3", ReportDates.label(end, true))
    }

    @Test
    fun leapDayAndPreviousWeekRemainCalendarDates() {
        val end = date("2028-03-02")
        assertTrue(date("2028-02-29") in ReportDates.weekEnding(end))
        val previous = ReportDates.shift(end, -7)
        assertEquals(date("2028-02-24"), previous)
        assertEquals(ReportDates.weekEnding(end).first() - ReportDates.DAY_MILLIS, previous)
    }

    @Test
    fun selectedDateLabelsDoNotShiftWithLocalTimeZone() {
        val original = TimeZone.getDefault()
        try {
            TimeZone.setDefault(TimeZone.getTimeZone("America/Los_Angeles"))
            assertEquals("2026년 10월 6일 (화)", ReportDates.label(date("2026-10-06"), false))
        } finally {
            TimeZone.setDefault(original)
        }
    }

    @Test
    fun resumedSessionIncludesPreviousElapsedTimeAndFormatsHours() {
        assertEquals(8_500L, measurementElapsed(7_000L, 20_000L, 21_500L))
        assertEquals(7_000L, measurementElapsed(7_000L, 20_000L, 19_000L))
        assertEquals("01:01:01", measurementTime(3_661_000L))
    }

    @Test
    fun calendarWeekIsMondayToSundayEvenWhenSelectingMidweek() {
        val days = ReportDates.calendarWeek(date("2026-10-06"))
        assertEquals(date("2026-10-05"), days.first())
        assertEquals(date("2026-10-11"), days.last())
        assertEquals(7, days.size)
        assertEquals("2026.10.5 – 2026.10.11", ReportDates.label(date("2026-10-06"), true))
    }
}
